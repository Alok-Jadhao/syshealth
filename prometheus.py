"""SysHealth in Prometheus's text format, for GET /metrics.

SysHealth is not trying to replace a Prometheus setup. A team that already
has one can scrape this and keep its own dashboards and alerts, with what
Prometheus cannot work out for itself: the state each machine is in against
researched thresholds, and what the last load test says each size can hold
and which one to buy.

Names follow Prometheus conventions: base units (bytes, seconds) and 0-1
ratios, not percent. "waiting" is PSI's `some` and "stalled" its `full`, the
same words node_exporter uses for the raw counters.
"""

from analyzer import THRESHOLDS

MIB = 1024 * 1024
STATES = ("healthy", "degraded", "critical")


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels):
    if not labels:
        return ""
    return "{%s}" % ",".join('%s="%s"' % (k, _escape(v)) for k, v in labels.items() if v is not None)


def _number(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


class _Out:
    """Samples gathered by metric, because the format wants each metric's
    lines in one group, under one HELP and TYPE, however they were found."""

    def __init__(self):
        self.families = {}  # name -> (help, kind, [sample lines]), in first-seen order

    def metric(self, name, kind, help_text, value, labels=None):
        if value is None:
            return
        family = self.families.setdefault(name, (help_text, kind, []))
        family[2].append("%s%s %s" % (name, _labels(labels), _number(value)))

    def text(self):
        lines = []
        for name, (help_text, kind, samples) in self.families.items():
            lines.append("# HELP %s %s" % (name, help_text))
            lines.append("# TYPE %s %s" % (name, kind))
            lines.extend(samples)
        return "\n".join(lines) + "\n"


def _ratio(pct):
    return None if pct is None else float(pct) / 100.0


def render(snapshot, report=None):
    """The exposition text for every instance, and the last test's verdict."""
    out = _Out()
    for line, value in sorted(THRESHOLDS.items()):
        out.metric("syshealth_threshold_ratio", "gauge",
                   "Where each state begins, as a share of time (docs/thresholds.md).",
                   _ratio(value), {"line": line})

    for inst in snapshot:
        samples = inst["samples"]
        if not samples:
            continue
        latest = samples[-1]
        base = {
            "hostname": inst["hostname"],
            "instance_type": inst.get("instance_type"),
            "instance_id": latest.get("instance_id"),
            "region": latest.get("region"),
        }
        out.metric("syshealth_up", "gauge",
                   "1 if the agent has reported in the last 15 seconds.", bool(inst.get("online")), base)
        seen = latest.get("received_ts")
        out.metric("syshealth_last_seen_timestamp_seconds", "gauge",
                   "When the server last heard from the agent.", seen, base)

        out.metric("syshealth_memory_waiting_ratio", "gauge",
                   "Share of the last 10 s in which at least one task waited for memory (PSI some avg10).",
                   _ratio(latest.get("psi")), base)
        out.metric("syshealth_memory_waiting_minute_ratio", "gauge",
                   "The same, averaged over the last minute: what the state is judged on.",
                   _ratio(latest.get("avg_psi")), base)
        out.metric("syshealth_memory_stalled_ratio", "gauge",
                   "Share of the last 10 s in which every task waited for memory at once (PSI full avg10).",
                   _ratio(latest.get("psi_full")), base)

        state = str(latest.get("state") or "").lower()
        for name in STATES:
            out.metric("syshealth_state", "gauge",
                       "1 for the state the machine is in, 0 for the others.",
                       state == name, dict(base, state=name))

        pressure = latest.get("pressure") or {}
        out.metric("syshealth_cpu_waiting_ratio", "gauge",
                   "Share of the last 10 s in which at least one task waited for CPU (PSI).",
                   _ratio(pressure.get("cpu")), base)
        out.metric("syshealth_io_waiting_ratio", "gauge",
                   "Share of the last 10 s in which at least one task waited for disk I/O (PSI).",
                   _ratio(pressure.get("io")), base)
        out.metric("syshealth_io_stalled_ratio", "gauge",
                   "Share of the last 10 s in which every task waited for disk I/O at once (PSI).",
                   _ratio(pressure.get("io_full")), base)

        memory = latest.get("memory") or {}
        total = memory.get("total_mb") or latest.get("mem_total_mb")
        out.metric("syshealth_memory_total_bytes", "gauge", "RAM as the kernel sees it.",
                   None if total is None else int(total) * MIB, base)
        for part in ("programs", "cache", "other", "free"):
            value = memory.get(part + "_mb")
            out.metric("syshealth_memory_bytes", "gauge",
                       "Where the RAM is: programs, file cache, kernel and other, free.",
                       None if value is None else int(value) * MIB, dict(base, part=part))
        out.metric("syshealth_memory_in_use_ratio", "gauge",
                   "(MemTotal - MemAvailable) / MemTotal: what most dashboards call used.",
                   _ratio(memory.get("used_pct")), base)
        if memory.get("swap_total_mb") is not None:
            out.metric("syshealth_swap_used_bytes", "gauge", "Swap in use.",
                       int(memory.get("swap_used_mb") or 0) * MIB, base)

        app = latest.get("app")
        if app and app.get("psi") is not None:
            labels = dict(base, app=app.get("name"))
            out.metric("syshealth_app_memory_waiting_ratio", "gauge",
                       "Share of the last 10 s in which a task in the app's cgroup waited for memory.",
                       _ratio(app.get("psi")), labels)
            out.metric("syshealth_app_memory_stalled_ratio", "gauge",
                       "Share of the last 10 s in which every task in the app's cgroup waited at once.",
                       _ratio(app.get("psi_full")), labels)
            if app.get("current_mb") is not None:
                out.metric("syshealth_app_memory_bytes", "gauge", "Memory the app's cgroup holds.",
                           int(app["current_mb"]) * MIB, labels)
            if app.get("limit_mb"):
                out.metric("syshealth_app_memory_limit_bytes", "gauge",
                           "The app cgroup's memory limit (memory.max).", int(app["limit_mb"]) * MIB, labels)
            if app.get("oom_kills") is not None:
                out.metric("syshealth_app_oom_kills_total", "counter",
                           "Processes the kernel killed in the app's cgroup for going over its limit.",
                           int(app["oom_kills"]), labels)

        test = latest.get("load_test") if inst.get("online") else None
        out.metric("syshealth_load_test_bytes", "gauge",
                   "Memory the running load test asks this machine to hold; 0 when none runs.",
                   int((test or {}).get("load_mb") or 0) * MIB, base)

    if report and report.get("rows") and not report.get("running"):
        _right_sizing(out, report)
    return out.text()


def _right_sizing(out, report):
    test = {"test_id": report.get("test_id")}
    if report.get("need_mb"):
        out.metric("syshealth_app_need_bytes", "gauge",
                   "What the app needs at its busiest, as given for the last load test.",
                   int(report["need_mb"]) * MIB, test)
        out.metric("syshealth_app_margin_ratio", "gauge",
                   "Room to keep spare above that, as a share of it.",
                   _ratio(report.get("margin_pct")), test)
    pick = (report.get("recommendation") or {}).get("pick")
    for row in report["rows"]:
        labels = dict(test, hostname=row["hostname"], instance_type=row["instance_type"])
        cap = row.get("capacity")
        if cap:
            out.metric("syshealth_capacity_bytes", "gauge",
                       "The most memory this size held in the last test while staying healthy. "
                       "kind is estimate, at_least (never slowed down) or below (slowed at the lowest level).",
                       int(cap["mb"]) * MIB, dict(labels, kind=cap["kind"]))
        out.metric("syshealth_recommended", "gauge",
                   "1 for the size the last test recommends: the cheapest that holds the app with room to spare.",
                   row["hostname"] == pick, labels)
        if row.get("price"):
            out.metric("syshealth_price_usd_per_hour", "gauge",
                       "On-demand list price of this size, or your own from SYSHEALTH_PRICES_FILE.",
                       row["price"]["usd_hour"], labels)
