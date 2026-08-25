"""Terminal output.

Stdlib only, and colour is disabled automatically when stdout is not a TTY or
``NO_COLOR`` is set, so piping into a file or a CI log stays readable.
"""

from __future__ import annotations

import os
import sys

from .analysis import RunSummary, State
from .models import Interval
from .rightsize import Confidence, Sizing, Verdict

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}

STATE_COLOUR = {
    State.HEALTHY: "green",
    State.DEGRADED: "yellow",
    State.SATURATED: "red",
    State.UNKNOWN: "grey",
}

SIZING_COLOUR = {
    Sizing.OVERSIZED: "cyan",
    Sizing.RIGHT_SIZED: "green",
    Sizing.UNDERSIZED: "red",
    Sizing.INSUFFICIENT_DATA: "grey",
}


def colour_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("SYSHEALTH_FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, *styles: str, enabled: bool | None = None) -> str:
    if enabled is None:
        enabled = colour_enabled()
    if not enabled or not styles:
        return text
    prefix = "".join(_ANSI.get(s, "") for s in styles)
    return f"{prefix}{text}{_ANSI['reset']}"


def bar(pct: float, width: int = 24, cap: float = 20.0) -> str:
    """A stall bar. Capped low because the interesting range is 0-20%,
    not 0-100: a machine stalling 20% of the time is already unusable."""
    filled = int(min(max(pct, 0.0) / cap, 1.0) * width)
    return "█" * filled + "·" * (width - filled)


def format_live(sample: Interval, colour: bool | None = None) -> str:
    """One line per sample for ``syshealth watch``."""
    mem = sample.some("memory")
    io = sample.some("io")
    cpu = sample.some("cpu")

    from .analysis import classify

    state = classify(sample, "memory")
    tag = paint(f"{state.value:<9}", STATE_COLOUR[state], "bold", enabled=colour)

    return (
        f"{tag} "
        f"mem {mem:6.2f}%  {paint(bar(mem, 18), STATE_COLOUR[state], enabled=colour)}  "
        f"io {io:5.2f}%  cpu {cpu:5.2f}%  "
        f"{paint(f'util {sample.mem_used_pct:5.1f}%', 'grey', enabled=colour)}"
    )


def format_summary(summary: RunSummary, colour: bool | None = None) -> str:
    out: list[str] = []
    out.append("")
    out.append(paint("── measurement ─────────────────────────────────", "grey", enabled=colour))
    out.append(
        f"  duration    {summary.duration_s:.0f}s over {summary.samples} samples"
    )
    if summary.label:
        out.append(f"  label       {summary.label}")

    out.append("")
    out.append(paint("  SATURATION  (share of wall-clock time stalled)", "bold", enabled=colour))
    for name in ("memory", "cpu", "io"):
        res = summary.resources.get(name)
        if not res:
            continue
        col = STATE_COLOUR[res.state]
        out.append(
            f"    {name:<7} p50 {res.some_p50:6.2f}%  p95 {res.some_p95:6.2f}%  "
            f"max {res.some_max:6.2f}%   "
            f"{paint(bar(res.some_p95, 16), col, enabled=colour)} "
            f"{paint(res.state.value, col, enabled=colour)}"
        )

    out.append("")
    out.append(paint("  UTILISATION  (what a normal dashboard shows)", "bold", enabled=colour))
    out.append(
        f"    memory  used/free rule   peak {summary.mem_naive_used_pct_max:5.1f}%   "
        f"<- what most dashboards show"
    )
    out.append(
        f"            working set      peak {summary.mem_used_pct_max:5.1f}%   "
        f"of {summary.mem_total_kb / (1024 * 1024):.2f} GB"
    )
    out.append(
        f"    cpu     mean {summary.cpu_busy_pct_mean:5.1f}%  "
        f"peak {summary.cpu_busy_pct_max:5.1f}%"
    )

    # The gap is only a *false alarm* when the machine was actually healthy.
    # On a saturated box both numbers are high and the gap means nothing, so
    # saying "utilisation looked worse than reality" there would be a lie.
    mem = summary.resources.get("memory")
    gap = summary.divergence
    if mem is not None and abs(gap) > 1:
        naive = summary.mem_naive_used_pct_max
        if mem.state is State.HEALTHY and naive >= 80:
            note = (
                f"{naive:.0f}% \"used\" but only {mem.some_p95:.2f}% stalled — a "
                "utilisation alert would have fired here for nothing"
            )
            style = "cyan"
        elif mem.state is not State.HEALTHY and naive < 70:
            note = (
                f"only {naive:.0f}% \"used\" yet stalling {mem.some_p95:.2f}% — a "
                "utilisation alert would have missed this entirely"
            )
            style = "red"
        else:
            note = (
                f"{naive:.0f}% \"used\", {mem.some_p95:.2f}% stalled — "
                "both signals agree"
            )
            style = "grey"
        out.append("    " + paint(note, style, enabled=colour))

    if summary.direct_reclaim_total or summary.oom_kills or summary.swap_in_total:
        out.append("")
        out.append(paint("  RECLAIM", "bold", enabled=colour))
        if summary.direct_reclaim_total:
            out.append(
                f"    direct reclaim  {summary.direct_reclaim_total} pages "
                f"({summary.direct_reclaim_per_s:.0f}/s)"
            )
        if summary.major_faults_total:
            out.append(f"    major faults    {summary.major_faults_total}")
        if summary.swap_in_total:
            out.append(f"    swapped in      {summary.swap_in_total} pages")
        if summary.oom_kills:
            out.append(
                "    "
                + paint(f"OOM kills       {summary.oom_kills}", "red", "bold", enabled=colour)
            )

    return "\n".join(out)


def format_verdict(verdict: Verdict, colour: bool | None = None) -> str:
    col = SIZING_COLOUR[verdict.sizing]
    out: list[str] = ["", paint("── verdict ─────────────────────────────────────", "grey", enabled=colour)]

    conf = {
        Confidence.HIGH: "high confidence",
        Confidence.MEDIUM: "medium confidence",
        Confidence.LOW: "low confidence",
    }[verdict.confidence]

    out.append(
        "  "
        + paint(verdict.sizing.value, col, "bold", enabled=colour)
        + paint(f"   ({conf})", "grey", enabled=colour)
    )
    out.append(f"  {verdict.headline}")

    if verdict.current or verdict.recommended:
        out.append("")
        cur, rec = verdict.current, verdict.recommended
        if cur:
            out.append(
                f"    current      {cur.name:<12} {cur.ram_gb:>5g} GB  "
                f"{cur.vcpu:>2} vCPU   ${cur.usd_per_month:>7.2f}/mo"
            )
        if rec:
            marker = "recommended " if verdict.changed else "keep        "
            out.append(
                f"    {marker} "
                + paint(
                    f"{rec.name:<12} {rec.ram_gb:>5g} GB  {rec.vcpu:>2} vCPU   "
                    f"${rec.usd_per_month:>7.2f}/mo",
                    col if verdict.changed else "green",
                    enabled=colour,
                )
            )
        delta = verdict.monthly_delta_usd
        if verdict.changed and abs(delta) > 0.001:
            word = "saving" if delta < 0 else "extra"
            tone = "cyan" if delta < 0 else "yellow"
            out.append(
                "    "
                + paint(
                    f"{word:<12} ${abs(delta):.2f}/month  "
                    f"(${abs(delta) * 12:.2f}/year per instance)",
                    tone,
                    "bold",
                    enabled=colour,
                )
            )

    if verdict.reasons:
        out.append("")
        out.append(paint("  why", "bold", enabled=colour))
        for reason in verdict.reasons:
            out.extend(_wrap(reason, "    - "))

    if verdict.evidence:
        out.append("")
        out.append(paint("  evidence", "bold", enabled=colour))
        for item in verdict.evidence:
            out.append(paint(f"    {item}", "grey", enabled=colour))

    if verdict.caveats:
        out.append("")
        out.append(paint("  caveats", "bold", enabled=colour))
        for caveat in verdict.caveats:
            out.extend(_wrap(caveat, "    ! ", style="yellow", colour=colour))

    return "\n".join(out)


def _wrap(text: str, prefix: str, width: int = 74, style: str | None = None, colour=None) -> list[str]:
    import textwrap

    pad = " " * len(prefix)
    lines = textwrap.wrap(text, width=width) or [""]
    rendered = [prefix + lines[0]] + [pad + line for line in lines[1:]]
    if style:
        return [paint(line, style, enabled=colour) for line in rendered]
    return rendered


# --------------------------------------------------------------- incidents --

_STATUS_STYLE = {
    "RESOLVED": "green",
    "ESCALATED": "yellow",
    "AWAITING_APPROVAL": "yellow",
    "REMEDIATING": "cyan",
    "VERIFYING": "cyan",
}


def format_incident(report: dict, colour: bool | None = None) -> str:
    """The whole story of one incident, for a terminal.

    Deliberately the same content the dashboard shows and the API returns.
    Three questions have to be answerable from this alone: why the system did
    what it did, on what evidence, and what happened next.
    """
    incident = report["incident"]
    out: list[str] = []

    status = incident["status"]
    out.append("")
    out.append(
        paint(f"  {incident['id']}  ", "bold", enabled=colour)
        + paint(status, _STATUS_STYLE.get(status, "grey"), enabled=colour)
        + paint(f"  {incident['severity']}", "grey", enabled=colour)
    )
    out.append(paint(f"  {incident['title']}", "bold", enabled=colour))
    out.append(
        paint(
            f"  node {incident['node']} · {incident['age_s']:.0f}s · "
            f"mode {incident['mode']} · {incident['attempts']} remediation attempt(s)",
            "grey",
            enabled=colour,
        )
    )

    for diagnosis in report.get("diagnoses", []):
        out.append("")
        out.append(paint("  DIAGNOSIS", "bold", enabled=colour))
        out.extend(_wrap(diagnosis["cause"], "    "))
        cited = ", ".join(f"#{c}" for c in diagnosis["cites"])
        out.append(
            paint(
                f"    confidence {diagnosis['confidence']} · by {diagnosis['reasoner']} "
                f"· citing {cited}",
                "grey",
                enabled=colour,
            )
        )
        if diagnosis["observations"]:
            out.append("")
            out.append(paint("    observed", "bold", enabled=colour))
            for item in diagnosis["observations"]:
                out.extend(_wrap(item, "      - ", style="grey", colour=colour))
        if diagnosis["hypotheses"]:
            out.append("")
            out.append(paint("    inferred", "bold", enabled=colour))
            for item in diagnosis["hypotheses"]:
                out.extend(_wrap(item, "      - ", style="grey", colour=colour))

    if report.get("evidence"):
        out.append("")
        out.append(paint("  EVIDENCE", "bold", enabled=colour))
        for item in report["evidence"]:
            mark = " " if item["ok"] else "!"
            out.append(
                paint(
                    f"    {mark} #{item['id']:<4} {item['tool']}({_args(item['arguments'])})",
                    "grey",
                    enabled=colour,
                )
            )

    if report.get("actions"):
        out.append("")
        out.append(paint("  ACTIONS", "bold", enabled=colour))
        for item in report["actions"]:
            style = "green" if item["status"] == "SUCCEEDED" else (
                "red" if item["status"] in ("FAILED", "DENIED", "REJECTED") else "yellow"
            )
            label = f"{item['status']:<18}"
            out.append(
                f"    {paint(label, style, enabled=colour)} "
                f"{item['action']}({_args(item['arguments'])})  "
                + paint(f"[{item['tier']}]", "grey", enabled=colour)
            )
            out.extend(_wrap(f"why: {item['reason']}", "      ", style="grey", colour=colour))
            out.extend(_wrap(f"policy: {item['ruling']}", "      ", style="grey", colour=colour))

    for check in report.get("verifications", []):
        out.append("")
        out.append(
            paint("  VERIFICATION  ", "bold", enabled=colour)
            + paint(
                "RECOVERED" if check["recovered"] else "NOT RECOVERED",
                "green" if check["recovered"] else "red",
                enabled=colour,
            )
        )
        for item in check["checks"]:
            mark = "PASS" if item["passed"] else "FAIL"
            style = "green" if item["passed"] else "red"
            out.append(
                f"    {paint(mark, style, enabled=colour)}  {item['name']}: "
                + paint(item["observed"], "grey", enabled=colour)
            )

    out.append("")
    out.append(paint("  TIMELINE", "bold", enabled=colour))
    for event in incident["timeline"]:
        out.append(
            paint(f"    {event['clock']}  ", "grey", enabled=colour)
            + paint(f"{event['kind']:<13}", "cyan", enabled=colour)
            + event["message"]
        )

    if incident["resolution"]:
        out.append("")
        out.extend(_wrap(incident["resolution"], "  -> ", style="bold", colour=colour))

    out.append("")
    return "\n".join(out)


def _args(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in sorted(arguments.items()))
