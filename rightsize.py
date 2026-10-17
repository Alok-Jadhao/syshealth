"""Right-sizing: what a load test showed each size can hold, and which to buy.

A monitor reports that a machine was under pressure. This answers the question
the test was run for: how much memory can each size hold before the app slows
down, how much room does that leave above what the app needs, and which is the
cheapest size that leaves enough.

    capacity   The most memory a size held before it left the healthy band,
               read from the pressure at each load level of the test.
    headroom   How far that capacity is above the app's peak, as a share of
               the peak. The app needs room to grow, and the OS needs room
               for its cache; 25% unless the person says otherwise.
    verdict    The cheapest size whose capacity covers the peak plus that
               headroom, priced from pricing.py.

Everything here is computed from the samples the agents already push, so it
works the same for a live fleet, for dev.py's simulated one, and for anyone
calling GET /report instead of opening the dashboard.
"""

import loadtest
import pricing
from analyzer import THRESHOLDS

SETTLE_SEC = 10    # a step's first 10 s reading still carries the step before it
TRAIL_SEC = 60     # states are judged on a minute, so they trail the load
DEFAULT_MARGIN_PCT = 25
MAX_MARGIN_PCT = 200

STATE_RANK = {"UNKNOWN": 0, "HEALTHY": 1, "DEGRADED": 2, "CRITICAL": 3}
RANK_KEY = ["unknown", "healthy", "degraded", "critical"]


def _num(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN


def _ts(sample):
    return _num(sample.get("received_ts"), None) or _num(sample.get("timestamp"), 0.0)


def _test(sample):
    test = sample.get("load_test")
    return test if isinstance(test, dict) and test.get("test_id") else None


def _thresholds(samples):
    for sample in reversed(samples):
        th = sample.get("thresholds")
        if isinstance(th, dict) and _num(th.get("degraded")) is not None:
            return {
                "degraded": _num(th.get("degraded")),
                "critical": _num(th.get("critical"), THRESHOLDS["critical"]),
                "critical_full": _num(th.get("critical_full"), THRESHOLDS["critical_full"]),
            }
    return dict(THRESHOLDS)


def _latest(samples, key):
    for sample in reversed(samples):
        if sample.get(key) is not None:
            return sample.get(key)
    return None


def latest_test_id(snapshot):
    """The test any instance reported most recently."""
    newest, found = None, None
    for inst in snapshot:
        for sample in reversed(inst["samples"]):
            test = _test(sample)
            if test:
                if newest is None or _ts(sample) > newest:
                    newest, found = _ts(sample), str(test["test_id"])
                break
    return found


def band(level, th):
    """The state a load level's sustained pressure falls in."""
    if level["killed"]:
        return "CRITICAL"
    if level["some"] >= th["critical"] or level["full"] >= th["critical_full"]:
        return "CRITICAL"
    if level["some"] >= th["degraded"]:
        return "DEGRADED"
    return "HEALTHY"


def levels_of(during, th):
    """Sustained pressure at each load level of the test, smallest first.

    The mean of a step's readings, so one spike does not condemn a level.
    Each step's first 10 s are skipped: the 10-second reading still carries
    the step before it, which on the way down a wave would blame a low level
    for a high one's pressure. A level met twice (a wave, or bursts) is judged
    by its worse visit.

    A level is also failed if the kernel killed the test process during it.
    Once stress is killed the memory is freed and the pressure falls, so its
    readings alone would make the level look fine.
    """
    steps = {}
    order = []
    for sample in during:
        step = _test(sample).get("step")
        if step not in steps:
            steps[step] = []
            order.append(step)
        steps[step].append(sample)

    levels = {}
    killed_before = 0
    for step in order:
        samples = steps[step]
        first = _ts(samples[0])
        settled = [s for s in samples if _ts(s) >= first + SETTLE_SEC] or samples
        some = sum(_num(s.get("psi"), 0.0) for s in settled) / len(settled)
        full = sum(_num(s.get("psi_full"), 0.0) for s in settled) / len(settled)
        killed_here = max(int(_num(_test(s).get("stress_killed"), 0)) for s in samples)
        killed = killed_here > killed_before
        killed_before = max(killed_before, killed_here)

        mb = int(_num(_test(samples[0]).get("load_mb"), 0))
        level = levels.setdefault(mb, {"mb": mb, "some": 0.0, "full": 0.0, "killed": False})
        level["some"] = max(level["some"], some)
        level["full"] = max(level["full"], full)
        level["killed"] = level["killed"] or killed

    out = sorted(levels.values(), key=lambda lv: lv["mb"])
    for level in out:
        level["some"] = round(level["some"], 2)
        level["full"] = round(level["full"], 2)
        level["state"] = band(level, th)
    return out


def capacity(levels, th):
    """The most memory this size held while staying healthy.

    kind:
      estimate  between the last level it held and the first it did not. The
                crossing is interpolated on the pressure; pressure rises faster
                than linearly with load, so a straight line between the two
                levels crosses the threshold early, and the estimate errs low.
      at_least  it never slowed down: the most the test asked of it.
      below     it slowed down even at the lowest level.
    """
    if not levels:
        return None
    first_bad = None
    for i, level in enumerate(levels):
        if level["state"] != "HEALTHY":
            first_bad = i
            break
    if first_bad is None:
        top = levels[-1]["mb"]
        return {"mb": top, "kind": "at_least", "held_mb": top, "failed_mb": None}
    bad = levels[first_bad]
    if first_bad == 0:
        return {"mb": bad["mb"], "kind": "below", "held_mb": None, "failed_mb": bad["mb"]}

    good = levels[first_bad - 1]
    frac = 0.0
    if not bad["killed"]:
        # Where each line was crossed, as a fraction of the way from good to bad.
        crossings = []
        if bad["some"] >= th["degraded"] and bad["some"] > good["some"]:
            crossings.append((th["degraded"] - good["some"]) / (bad["some"] - good["some"]))
        if bad["full"] >= th["critical_full"] and bad["full"] > good["full"]:
            crossings.append((th["critical_full"] - good["full"]) / (bad["full"] - good["full"]))
        if crossings:
            frac = max(0.0, min(1.0, min(crossings)))
    # A killed level has no pressure worth reading: say only what it held.
    mb = good["mb"] + frac * (bad["mb"] - good["mb"])
    return {"mb": int(round(mb)), "kind": "estimate",
            "held_mb": good["mb"], "failed_mb": bad["mb"]}


def fit(cap, need_mb, required_mb):
    """How a size's capacity compares with what the app needs.

      fits        holds the peak plus the headroom
      unmeasured  held the peak, but the test stopped before the headroom
      tight       holds the peak, with less headroom than asked for
      too_small   slows down before the app's peak
      untested    the test never reached the app's peak, so it cannot say
    """
    if cap is None:
        return "untested"
    mb, kind = cap["mb"], cap["kind"]
    if kind == "estimate":
        if mb >= required_mb:
            return "fits"
        return "tight" if mb >= need_mb else "too_small"
    if kind == "at_least":
        if mb >= required_mb:
            return "fits"
        return "unmeasured" if mb >= need_mb else "untested"
    return "too_small" if need_mb >= mb else "untested"


def spare_pct(cap, need_mb):
    if cap is None or cap["kind"] == "below" or not need_mb:
        return None
    return round((cap["mb"] - need_mb) * 100.0 / need_mb)


def _outcome(row, running, variable):
    """The table's one-line result: how the size coped with the whole test."""
    if row["killed"]:
        return "critical", "Ran out of memory: the kernel killed the test"
    key = RANK_KEY[row["rank"]]
    text = {"critical": "Could not cope", "degraded": "Coped, but slowed down",
            "healthy": "Coped comfortably", "unknown": "Too early to tell"}[key]
    if variable and key == "critical" and row["critical_from"] is not None:
        text = "Could not cope from " + loadtest.format_mb(row["critical_from"])
    elif variable and key == "degraded" and row["degraded_from"] is not None:
        text = "Slowed down from " + loadtest.format_mb(row["degraded_from"])
    elif running and key == "healthy":
        text = "No trouble so far"
    return key, text


def analyse_instance(inst, test_id):
    """One instance's part in one test, or None if it took no part."""
    samples = inst["samples"]
    during = [s for s in samples if _test(s) and str(_test(s)["test_id"]) == test_id]
    if not during:
        return None
    th = _thresholds(samples)
    start, end = _ts(during[0]), _ts(during[-1])
    latest = samples[-1]
    running = bool(inst.get("online") and _test(latest) and str(_test(latest)["test_id"]) == test_id)

    # The state is judged on a minute and must hold for 15 s, so it trails
    # the load: look a minute past the end for the worst of it.
    worst_rank, worst_state, worst_minute = 0, "UNKNOWN", 0.0
    for s in samples:
        t = _ts(s)
        if start <= t <= end + TRAIL_SEC:
            rank = STATE_RANK.get(str(s.get("state", "")).upper(), 0)
            if rank > worst_rank:
                worst_rank, worst_state = rank, str(s.get("state")).upper()
            worst_minute = max(worst_minute, _num(s.get("avg_psi"), 0.0))

    worst_10s = max(_num(s.get("psi"), 0.0) for s in during)
    killed = max(int(_num(_test(s).get("stress_killed"), 0)) for s in during)
    levels = levels_of(during, th)

    degraded_from = next((lv["mb"] for lv in levels if lv["state"] != "HEALTHY"), None)
    critical_from = next((lv["mb"] for lv in levels if lv["state"] == "CRITICAL"), None)
    level_rank = 3 if critical_from is not None else 2 if degraded_from is not None else 1 if levels else 0

    itype = inst.get("instance_type") or "unknown"
    region = _latest(samples, "region")
    return {
        "hostname": inst["hostname"],
        "instance_type": itype,
        "mem_total_mb": _latest(samples, "mem_total_mb"),
        "region": region,
        "price": pricing.lookup(itype, region),
        "thresholds": th,
        "running": running,
        # How bad it got: the worse of the states it reported and the sustained
        # pressure at its worst load level. With short steps the one-minute
        # state never catches up with a level that was plainly too much.
        "rank": max(STATE_RANK.get(worst_state, 0), level_rank),
        "worst_state": worst_state,
        "worst_minute": round(worst_minute, 2),
        "worst_10s": round(worst_10s, 2),
        "killed": killed,
        "levels": levels,
        "degraded_from": degraded_from,
        "critical_from": critical_from,
        "capacity": capacity(levels, th),
        "_test": _test(during[-1]),
    }


def _money(usd):
    return "$%.2f" % usd if usd < 100 else "$%s" % format(int(round(usd)), ",")


def _cap_words(cap):
    if cap["kind"] == "at_least":
        return loadtest.format_mb(cap["mb"])
    return "about " + loadtest.format_mb(cap["mb"])


def _month(row):
    return row["price"]["usd_month"] if row.get("price") else None


def _exact_month(row):
    """Unrounded, so a difference of two prices is not a cent out."""
    return row["price"]["usd_hour"] * pricing.HOURS_PER_MONTH


def _join(names):
    names = list(names)
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def recommend(rows, need_mb, margin_pct):
    """The size to buy, said the way a person would decide it."""
    required = need_mb * (1 + margin_pct / 100.0)
    need_s = loadtest.format_mb(need_mb)
    tested = [r for r in rows if r["capacity"]]
    if not tested:
        return None

    preference = {"fits": 0, "unmeasured": 1, "tight": 2}

    def cost_key(row):
        month = _month(row)
        return (month if month is not None else float("inf"),
                row.get("mem_total_mb") or float("inf"))

    candidates = sorted((r for r in tested if r["fit"] in preference),
                        key=lambda r: (preference[r["fit"]],) + cost_key(r))
    out = {"pick": None, "headline": "", "detail": "", "lines": []}

    if not candidates:
        short = [r for r in tested if r["fit"] == "untested"]
        if short:
            # Sizes that never slowed down, but were never asked for as much
            # as the app needs: the test, not the size, ran out.
            out["headline"] = "The test stopped short of your app's %s" % need_s
            out["detail"] = ("%s held %s, the most the test asked of %s, without slowing down. "
                             "Run it again up to at least %s to see which will cope."
                             % (_join([r["instance_type"] for r in short]),
                                loadtest.format_mb(short[0]["capacity"]["mb"]),
                                "it" if len(short) == 1 else "them",
                                loadtest.format_mb(int(round(required)))))
            return out
        biggest = max(tested, key=lambda r: r["capacity"]["mb"])
        out["headline"] = "None of the tested sizes holds your app's %s" % need_s
        out["detail"] = ("The most any of them held before slowing down was %s, on %s. "
                         "Test a bigger size." % (_cap_words(biggest["capacity"]),
                                                  biggest["instance_type"]))
        return out

    pick = candidates[0]
    cap = pick["capacity"]
    month = _month(pick)
    out["pick"] = pick["hostname"]
    out["headline"] = "Choose %s%s" % (
        pick["instance_type"], " · %s a month" % _money(month) if month is not None else "")

    if pick["fit"] == "fits" and cap["kind"] == "estimate":
        out["detail"] = ("It holds %s before your app slows down: %d%% more than your app's %s, "
                         "and over the %d%% you want spare."
                         % (_cap_words(cap), pick["spare_pct"], need_s, margin_pct))
    elif pick["fit"] == "fits" and pick["spare_pct"] > 0:
        out["detail"] = ("It held %s, the most this test asked of it, without slowing down: at "
                         "least %d%% more than your app's %s." % (_cap_words(cap), pick["spare_pct"], need_s))
    elif pick["fit"] == "fits":
        out["detail"] = "It held your app's %s without slowing down." % need_s
    elif pick["fit"] == "unmeasured":
        out["detail"] = ("It held your app's %s without slowing down, but the test went no "
                         "higher, so the room left is not measured. To see it, ramp up past %s."
                         % (need_s, loadtest.format_mb(int(round(required)))))
    else:
        out["detail"] = ("It holds %s before slowing down, only %d%% more than your app's %s: "
                         "less than the %d%% you want spare, and nothing tested leaves more."
                         % (_cap_words(cap), pick["spare_pct"], need_s, margin_pct))

    # What the alternatives would have meant, in the same money.
    if month is not None:
        cheaper = [r for r in tested if _month(r) is not None and _month(r) < month]
        if cheaper:
            nearest = max(cheaper, key=_month)
            ncap = nearest["capacity"]
            if nearest["fit"] == "tight":
                why = "it leaves only %d%% spare" % nearest["spare_pct"]
            elif ncap["kind"] == "estimate":
                why = "it holds only %s before slowing down" % _cap_words(ncap)
            elif ncap["kind"] == "below" and len(nearest["levels"]) > 1:
                why = "it slowed down at %s, the lowest level tested" % loadtest.format_mb(ncap["mb"])
            elif ncap["kind"] == "below":
                why = "it slowed down holding %s" % loadtest.format_mb(ncap["mb"])
            else:
                why = "the test did not show it holding your app with room to spare"
            out["lines"].append("%s would save %s a month, but %s." % (
                nearest["instance_type"], _money(_exact_month(pick) - _exact_month(nearest)), why))
        dearer = [r for r in tested if _month(r) is not None and _month(r) > month]
        if dearer:
            biggest = max(dearer, key=_month)
            extra = _exact_month(biggest) - _exact_month(pick)
            out["lines"].append("Against %s, the biggest size tested, that saves %s a month (%s a year)."
                                % (biggest["instance_type"], _money(extra), _money(extra * 12)))
    return out


def report(snapshot, test_id=None, need_mb=None, margin_pct=None, plans=None):
    """Everything the dashboard's results panel and GET /report show."""
    test_id = test_id or latest_test_id(snapshot)
    if not test_id:
        return {"test_id": None}

    rows = [r for r in (analyse_instance(inst, test_id) for inst in snapshot) if r]
    if not rows:
        return {"test_id": test_id, "error": "No instance has reported this test."}

    recorded = (plans or {}).get(test_id) or {}
    plan = recorded.get("plan")
    info = rows[0]["_test"]
    mode = (plan or {}).get("mode") or info.get("mode") or "peak"
    variable = mode == "variable"
    peak = (plan or {}).get("peak_mb") or max(lv["mb"] for r in rows for lv in r["levels"]) or None
    running = any(r["running"] for r in rows)

    # What the person said their app needs when they planned the test; else
    # the most the test asked for, which is what a peak test means anyway.
    default_need = (plan or {}).get("need_mb") or peak
    need = int(need_mb) if need_mb else default_need
    margin = DEFAULT_MARGIN_PCT if margin_pct is None else int(margin_pct)
    required = need * (1 + margin / 100.0) if need else None

    for row in rows:
        row["fit"] = fit(row["capacity"], need, required) if need else "untested"
        row["spare_pct"] = spare_pct(row["capacity"], need)
        key, text = _outcome(row, row["running"], variable)
        row["outcome"] = {"key": key, "text": text}

    if running:
        verdict = {"pick": None, "headline": "Test in progress",
                   "detail": "The recommendation appears when the test finishes. States lag "
                             "the load by up to a minute.", "lines": []}
    elif need:
        verdict = recommend(rows, need, margin)
    else:
        verdict = None

    for row in rows:
        row.pop("_test", None)

    priced = [r["price"] for r in rows if r.get("price")]
    regions = sorted({p["region"] for p in priced})
    own = any(p["source"] == "your prices file" for p in priced)
    return {
        "test_id": test_id,
        "running": running,
        "mode": mode,
        "pattern": (plan or {}).get("pattern") or info.get("pattern"),
        "summary": (plan or {}).get("summary") or info.get("summary") or "",
        "peak_mb": peak,
        "default_need_mb": default_need,
        "need_mb": need,
        "margin_pct": margin,
        "required_mb": int(round(required)) if required else None,
        "rows": rows,
        "recommendation": verdict,
        "prices": {
            "regions": regions,
            "fetched": pricing.fetched(),
            "note": ("Prices from your prices file where it has them, otherwise " if own else "") +
                    "AWS on-demand list price, Linux, %s, as of %s. Discounts, savings "
                    "plans and storage are not included." % (
                        ", ".join(regions) or "no region", pricing.fetched() or "an unknown date"),
        },
    }
