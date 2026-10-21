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
    cpu        Optional: the app's average CPU, in vCPUs. A size needs that
               many (plus the headroom), and a burstable one is priced with
               the CPU credits it would burn above its baseline.

Everything here is computed from the samples the agents already push, so it
works the same for a live fleet, for dev.py's simulated one, and for anyone
calling GET /report instead of opening the dashboard.
"""

import loadtest
import pricing
from analyzer import THRESHOLDS

SETTLE_SEC = 10    # a step's first 10 s reading still carries the step before it
TRAIL_SEC = 60     # states are judged on a minute, so they trail the load
DROPOUT_SEC = 15   # this much of the test still to run at a size's last reading: it dropped out
DEFAULT_MARGIN_PCT = 25
MAX_MARGIN_PCT = 200
MAX_CPU_VCPU = 192   # the most vCPUs any size in the price list has

# The rule SysHealth is set against: judge a size by how much of its memory is
# "in use" (MemTotal - MemAvailable, what `free` and most dashboards show) and
# alarm at 80%, a common default for memory alarms.
USAGE_ALARM_PCT = 80

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
        used = [_num((s.get("memory") or {}).get("used_pct")) for s in settled]
        used = [u for u in used if u is not None]
        killed_here = max(int(_num(_test(s).get("stress_killed"), 0)) for s in samples)
        killed = killed_here > killed_before
        killed_before = max(killed_before, killed_here)

        mb = int(_num(_test(samples[0]).get("load_mb"), 0))
        level = levels.setdefault(mb, {"mb": mb, "some": 0.0, "full": 0.0, "killed": False,
                                       "used_pct": None})
        level["some"] = max(level["some"], some)
        level["full"] = max(level["full"], full)
        level["killed"] = level["killed"] or killed
        if used:
            # "In use" as most dashboards count it, for the usage-rule comparison.
            level["used_pct"] = round(max(level["used_pct"] or 0, sum(used) / len(used)), 1)

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


def _vcpu(value):
    return ("%.2f" % value).rstrip("0").rstrip(".") + " vCPU"


def cpu_check(itype, cpu, margin_pct):
    """Whether a size's CPU carries the app's average, and what that costs.

    Not measured by the test, which loads memory: worked out from the size's
    vCPUs and, for a burstable size, its CPU credits. `cpu` is the app's
    average use in vCPUs (0.5 is half of one vCPU kept busy around the clock);
    the margin applies as it does to memory.
    """
    required = cpu * (1 + margin_pct / 100.0)
    spec = pricing.specs(itype)
    burst = pricing.burst(itype)
    out = {"vcpus": spec["vcpu"] if spec else None,
           "baseline_vcpu": burst["baseline_vcpu"] if burst else None,
           "mode": burst["mode"] if burst else None,
           "fits": None, "extra_usd_month": 0.0, "text": ""}
    if not spec:
        out["text"] = "vCPUs not known for this size"
        return out
    if required > spec["vcpu"]:
        out["fits"] = False
        out["text"] = "%d vCPU%s, short of the %s your app needs with %d%% spare" % (
            spec["vcpu"], "" if spec["vcpu"] == 1 else "s", _vcpu(required), margin_pct)
        return out
    out["fits"] = True
    if not burst:
        out["text"] = "%d vCPUs, no CPU credits to run out" % spec["vcpu"]
        return out
    baseline = burst["baseline_vcpu"]
    if burst["mode"] == "standard":
        # A t2 by default: once its saved credits run out it is held down.
        if required > baseline:
            out["fits"] = False
            out["text"] = ("held to %s once its CPU credits run out (standard mode, the %s "
                           "default), short of the %s your app needs" % (
                               _vcpu(baseline), itype.split(".")[0], _vcpu(required)))
        else:
            out["text"] = "within its CPU credits (%s on average)" % _vcpu(baseline)
        return out
    over = max(0.0, cpu - baseline)
    out["extra_usd_month"] = round(over * burst["usd_per_vcpu_hour"] * pricing.HOURS_PER_MONTH, 2)
    if over > 0:
        out["text"] = "%s above its %s baseline: about %s a month in CPU credits" % (
            _vcpu(over), _vcpu(baseline), _money(out["extra_usd_month"]))
    else:
        out["text"] = "within its %s baseline: no CPU credit charges" % _vcpu(baseline)
    return out


def _outcome(row, running, variable):
    """The table's one-line result: how the size coped with the whole test."""
    dropped = row.get("dropped")
    where = ""
    if dropped:
        where = ("at step %s of %s" % (dropped["step"], dropped["steps"]) if variable
                 else "with %ds of the test to go" % dropped["left_sec"])
    if row["killed"]:
        text = "Ran out of memory: the kernel killed the test"
        return "critical", text + (", then it stopped reporting " + where if dropped else "")
    key = RANK_KEY[row["rank"]]
    text = {"critical": "Could not cope", "degraded": "Coped, but slowed down",
            "healthy": "Coped comfortably", "unknown": "Too early to tell"}[key]
    if variable and key == "critical" and row["critical_from"] is not None:
        text = "Could not cope from " + loadtest.format_mb(row["critical_from"])
    elif variable and key == "degraded" and row["degraded_from"] is not None:
        text = "Slowed down from " + loadtest.format_mb(row["degraded_from"])
    elif running and key == "healthy":
        text = "No trouble so far"
    if dropped:
        if key in ("healthy", "unknown"):
            # It cannot be said to have coped with a test it did not finish.
            return "unknown", "Stopped reporting " + where
        text += ", then stopped reporting " + where
    return key, text


def analyse_instance(inst, test_id, stopped_at=None):
    """One instance's part in one test, or None if it took no part.

    stopped_at: when someone stopped the test, if they did.
    """
    samples = inst["samples"]
    during = [s for s in samples if _test(s) and str(_test(s)["test_id"]) == test_id]
    if not during:
        return None
    th = _thresholds(samples)
    start, end = _ts(during[0]), _ts(during[-1])
    latest = samples[-1]
    running = bool(inst.get("online") and _test(latest) and str(_test(latest)["test_id"]) == test_id)

    # Its last reading of the test still had time to run, and nobody pressed
    # Stop: the size dropped out (its agent restarted, or could not reach the
    # server). Said, so the steps it missed do not pass for a finished test.
    last = _test(during[-1])
    left = _num(last.get("remaining_sec"), 0.0)
    dropped = None
    if not running and left > DROPOUT_SEC and not (stopped_at and stopped_at <= end + DROPOUT_SEC):
        dropped = {"step": last.get("step"), "steps": last.get("steps"),
                   "load_mb": last.get("load_mb"), "left_sec": int(round(left))}

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
        "dropped": dropped,
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


def _cpu_extra(row):
    return (row.get("cpu") or {}).get("extra_usd_month") or 0.0


def _month(row):
    """What the size costs a month, with any CPU credits it would burn."""
    return round(row["price"]["usd_month"] + _cpu_extra(row), 2) if row.get("price") else None


def _exact_month(row):
    """Unrounded, so a difference of two prices is not a cent out."""
    return row["price"]["usd_hour"] * pricing.HOURS_PER_MONTH + _cpu_extra(row)


def _cpu_ok(row):
    return (row.get("cpu") or {}).get("fits") is not False


def _join(names):
    names = list(names)
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def untested_cheaper(pick, rows, cpu, margin_pct, limit=2):
    """Sizes with the pick's memory that would cost less, but the test did not
    run: mostly the same size with another maker's processor (t4g and t3a for a
    t3). Priced in the pick's region, CPU credits included when the app's CPU
    is given; a size whose CPU falls short is left out."""
    if not pick.get("price"):
        return []
    tested = {r["instance_type"] for r in rows}
    month = _exact_month(pick)
    out = []
    for price in pricing.same_memory(pick["instance_type"], pick["price"]["region"]):
        itype = price["instance_type"]
        check = cpu_check(itype, cpu, margin_pct) if cpu else None
        if itype in tested or (check and check["fits"] is False):
            continue
        total = price["usd_hour"] * pricing.HOURS_PER_MONTH + (check["extra_usd_month"] if check else 0.0)
        if total < month - 0.005:
            out.append({"instance_type": itype, "usd_month": round(total, 2),
                        "processor": pricing.processor(itype)})
    return sorted(out, key=lambda r: (r["usd_month"], r["instance_type"]))[:limit]


def _untested_line(pick, untested):
    gib = pricing.specs(pick["instance_type"])["memory_gib"]
    named = ["%s (%s) at %s" % (u["instance_type"], u["processor"], _money(u["usd_month"]))
             for u in untested]
    named[0] += " a month"
    line = ("Not tested, with the same %g GiB: %s. Add %s to a test before switching"
            % (gib, _join(named), "it" if len(untested) == 1 else "them"))
    if pricing.processor(pick["instance_type"]) != "Graviton" and \
            any(u["processor"] == "Graviton" for u in untested):
        line += "; Graviton is Arm, so your app needs an Arm build"
    return line + "."


def recommend(rows, need_mb, margin_pct, cpu_vcpu=None):
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

    candidates = sorted((r for r in tested if r["fit"] in preference and _cpu_ok(r)),
                        key=lambda r: (preference[r["fit"]],) + cost_key(r))
    out = {"pick": None, "headline": "", "detail": "", "lines": []}

    held = [r for r in tested if r["fit"] in preference]
    if not candidates and held:
        # Memory is not the problem: every size that holds it lacks the CPU.
        out["headline"] = "None of the sizes that hold your app's %s has the CPU for it" % need_s
        out["detail"] = "; ".join("%s: %s" % (r["instance_type"], r["cpu"]["text"])
                                  for r in sorted(held, key=cost_key)) + ". Test a bigger size."
        return out
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
    if _cpu_extra(pick):
        out["lines"].append("That includes about %s a month of CPU credits: your app's average is "
                            "above what %s earns." % (_money(_cpu_extra(pick)), pick["instance_type"]))

    if pick["fit"] == "fits" and cap["kind"] == "estimate":
        out["detail"] = ("It holds %s before your app slows down: %d%% more than your app's %s, "
                         "and over the %d%% you want spare."
                         % (_cap_words(cap), pick["spare_pct"], need_s, margin_pct))
    elif pick["fit"] == "fits" and pick["spare_pct"] > 0:
        out["detail"] = ("It held %s, %s, without slowing down: at least %d%% more than your "
                         "app's %s." % (_cap_words(cap),
                                        "before it stopped reporting" if pick.get("dropped")
                                        else "the most this test asked of it",
                                        pick["spare_pct"], need_s))
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
            if not _cpu_ok(nearest):
                why = "its CPU falls short: %s" % nearest["cpu"]["text"]
            elif nearest["fit"] == "tight":
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

    # What the test could not say: the same memory for less, on a size it did
    # not run. Named, not judged; only a test can say it holds the app.
    out["untested"] = untested_cheaper(pick, rows, cpu_vcpu, margin_pct)
    if out["untested"]:
        out["lines"].append(_untested_line(pick, out["untested"]))
    return out


def used_at(row, need_mb):
    """Memory in use while holding the app's need, as most dashboards count it.

    Interpolated between the load levels either side: memory in use grows in
    step with the memory held, unlike the waiting. A level where the kernel
    killed the test ran out of memory, so it counts as 100%. When the need is
    outside the levels tested, the answer is given only where it is certain
    either way: a level below the need already over the alarm line, or one
    above it still under.
    """
    points = sorted((lv["mb"], 100.0 if lv["killed"] else lv.get("used_pct"))
                    for lv in row["levels"])
    points = [(m, u) for m, u in points if u is not None]
    below = [p for p in points if p[0] <= need_mb]
    above = [p for p in points if p[0] >= need_mb]
    if below and above:
        (m0, u0), (m1, u1) = below[-1], above[0]
        if m1 == m0:
            return round(u0, 1)
        return round(min(100.0, u0 + (u1 - u0) * (need_mb - m0) / float(m1 - m0)), 1)
    if below and below[-1][1] >= USAGE_ALARM_PCT:
        return below[-1][1]
    if above and above[0][1] < USAGE_ALARM_PCT:
        return above[0][1]
    return None


def usage_rule(rows, need_mb, pick):
    """What a "memory over 80% in use" alarm would have chosen, beside our pick.

    Said either way. Where the two agree, that is worth knowing too; where
    they differ, the difference is the case for measuring waiting, not use.
    """
    judged = [r for r in rows if r.get("used_at_need") is not None]
    ours = next((r for r in rows if r["hostname"] == pick), None)
    if not ours or not judged:
        return None

    def cost(row):
        return (_month(row) if _month(row) is not None else float("inf"),
                row.get("mem_total_mb") or float("inf"))

    def in_use(row):
        return "%d%% in use" % int(round(row["used_at_need"]))

    rule = "a usage alarm (memory over %d%% in use)" % USAGE_ALARM_PCT
    need_s = loadtest.format_mb(need_mb)
    passing = sorted((r for r in judged if r["used_at_need"] < USAGE_ALARM_PCT), key=cost)
    if not passing:
        return {"pick": None, "agrees": False,
                "text": "Judged by memory in use, every size tested would trip %s at your app's "
                        "peak, %s included, though it holds %s before slowing down."
                        % (rule, ours["instance_type"], _cap_words(ours["capacity"]))}
    theirs = passing[0]
    if theirs["hostname"] == pick:
        return {"pick": pick, "agrees": True,
                "text": "Judged by memory in use, %s would pick the same size: %s is %s at "
                        "your app's peak." % (rule, ours["instance_type"], in_use(ours))}

    diff = None
    if _month(theirs) is not None and _month(ours) is not None:
        diff = _exact_month(theirs) - _exact_month(ours)
    if cost(theirs) > cost(ours):
        # The rule over-buys: ours looks full, but nothing is waiting yet.
        if ours.get("used_at_need") is not None:
            looks = "%s would be %s at your app's peak" % (ours["instance_type"], in_use(ours))
        else:
            looks = "%s looks too full by that rule" % ours["instance_type"]
        text = ("Judged by memory in use, %s would pick %s instead%s: %s, yet it holds %s "
                "before your app slows down." % (
                    rule, theirs["instance_type"],
                    ", %s a month more" % _money(diff) if diff else "",
                    looks, _cap_words(ours["capacity"])))
    else:
        # The rule under-buys: theirs looks roomy, but programs would wait.
        tcap = theirs["capacity"]
        if theirs["fit"] == "tight":
            why = "it leaves only %d%% spare" % theirs["spare_pct"]
        elif tcap and tcap["kind"] == "estimate":
            why = "it slows down from %s, short of your app's %s" % (_cap_words(tcap), need_s)
        else:
            why = "the test did not show it holding your app"
        text = ("Judged by memory in use, %s would pick %s%s, at %s with your app's peak, but %s."
                % (rule, theirs["instance_type"],
                   ", %s a month less" % _money(-diff) if diff else "",
                   in_use(theirs), why))
    return {"pick": theirs["hostname"], "agrees": False, "text": text}


def report(snapshot, test_id=None, need_mb=None, margin_pct=None, plans=None, cpu_vcpu=None):
    """Everything the dashboard's results panel and GET /report show."""
    test_id = test_id or latest_test_id(snapshot)
    if not test_id:
        return {"test_id": None}

    recorded = (plans or {}).get(test_id) or {}
    rows = [r for r in (analyse_instance(inst, test_id, recorded.get("stopped"))
                        for inst in snapshot) if r]
    if not rows:
        return {"test_id": test_id, "error": "No instance has reported this test."}

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

    cpu = float(cpu_vcpu) if cpu_vcpu else None
    for row in rows:
        row["cpu"] = cpu_check(row["instance_type"], cpu, margin) if cpu else None
        row["fit"] = fit(row["capacity"], need, required) if need else "untested"
        row["spare_pct"] = spare_pct(row["capacity"], need)
        row["used_at_need"] = used_at(row, need) if need else None
        key, text = _outcome(row, row["running"], variable)
        row["outcome"] = {"key": key, "text": text}

    if running:
        verdict = {"pick": None, "headline": "Test in progress",
                   "detail": "The recommendation appears when the test finishes. States lag "
                             "the load by up to a minute.", "lines": []}
    elif need:
        verdict = recommend(rows, need, margin, cpu)
        if verdict and verdict.get("pick"):
            verdict["usage_rule"] = usage_rule(rows, need, verdict["pick"])
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
        "cpu_vcpu": cpu,
        "rows": rows,
        "recommendation": verdict,
        "prices": {
            "regions": regions,
            "fetched": pricing.fetched(),
            "note": ("Prices from your prices file where it has them, otherwise " if own else "") +
                    "AWS on-demand list price, Linux, %s, as of %s. Discounts, savings "
                    "plans and storage are not included." % (
                        ", ".join(regions) or "no region", pricing.fetched() or "an unknown date") +
                    (" CPU credits: AWS's unlimited-mode rate for each vCPU-hour of your "
                     "app's average above a burstable size's baseline ($0.05 a vCPU-hour for "
                     "t2, t3 and t3a, $0.04 for t4g); not measured by this test." if cpu else ""),
        },
    }
