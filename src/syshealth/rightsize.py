"""Turn a measured run into a sizing verdict.

The rule this module exists to enforce: **a recommendation must always be
traceable to a measurement.** Every ``Verdict`` carries the evidence that
produced it, and the tool refuses to guess when the evidence is thin. A sizing
tool that confidently invents numbers is worse than no sizing tool, because
someone will act on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .analysis import RunSummary, State, Thresholds
from .catalog import Catalog, InstanceType


class Sizing(str, Enum):
    UNDERSIZED = "UNDERSIZED"
    RIGHT_SIZED = "RIGHT_SIZED"
    OVERSIZED = "OVERSIZED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Confidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class Policy:
    """Knobs governing how bold the recommendation is allowed to be."""

    # Spare RAM to leave above the observed peak working set when shrinking.
    # 30% absorbs growth and the fact that a short run may not have seen peak.
    headroom: float = 0.30

    # Runs shorter than this may not have observed a real peak, so the tool
    # will not recommend shrinking on their evidence alone.
    min_downsize_seconds: float = 300.0

    # A run must contain at least this many samples to say anything at all.
    min_samples: int = 5

    # Only propose a downsize if it actually saves meaningful money.
    min_monthly_saving_usd: float = 1.0


@dataclass
class Verdict:
    sizing: Sizing
    confidence: Confidence
    current: InstanceType | None = None
    recommended: InstanceType | None = None

    headline: str = ""
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    peak_working_set_gb: float = 0.0

    @property
    def monthly_delta_usd(self) -> float:
        """Negative means cheaper. Zero when either side is unknown."""
        if not self.current or not self.recommended:
            return 0.0
        return self.recommended.usd_per_month - self.current.usd_per_month

    @property
    def changed(self) -> bool:
        return bool(
            self.recommended and self.current and self.recommended.name != self.current.name
        )


def _working_set_gb(summary: RunSummary) -> float:
    """Peak memory genuinely in use, in GB.

    Derived from ``MemAvailable``, which the kernel computes as the memory an
    allocation could obtain without swapping — it already excludes reclaimable
    page cache. That is what makes this a working-set estimate and not the
    usual "used memory" figure that counts cache and panics everyone.
    """
    if summary.mem_total_kb <= 0:
        return 0.0
    return (summary.mem_used_pct_max / 100.0) * summary.mem_total_kb / (1024 * 1024)


def evaluate(
    summary: RunSummary,
    current_type: str | None = None,
    catalog: Catalog | None = None,
    policy: Policy | None = None,
    thresholds: Thresholds | None = None,
) -> Verdict:
    """Produce a sizing verdict for one measured run."""
    cat = catalog or Catalog()
    pol = policy or Policy()
    t = thresholds or Thresholds()

    current = cat.get(current_type)
    peak_ws = _working_set_gb(summary)
    memory = summary.resources.get("memory")
    cpu = summary.resources.get("cpu")

    verdict = Verdict(
        sizing=Sizing.INSUFFICIENT_DATA,
        confidence=Confidence.LOW,
        current=current,
        peak_working_set_gb=peak_ws,
    )

    # -- refuse to answer when the evidence is not there -------------------

    if not summary.psi_available:
        verdict.headline = "No verdict: this kernel does not expose PSI."
        verdict.reasons.append(
            "Saturation cannot be measured without /proc/pressure. "
            "Requires Linux 4.20+ with CONFIG_PSI=y, and on some distributions "
            "the psi=1 boot parameter."
        )
        return verdict

    if summary.samples < pol.min_samples:
        verdict.headline = (
            f"No verdict: only {summary.samples} samples "
            f"(need at least {pol.min_samples})."
        )
        verdict.reasons.append("Run for longer, or lower the sample interval.")
        return verdict

    # -- evidence, gathered once and attached to whatever we conclude ------

    verdict.evidence = _build_evidence(summary, peak_ws)

    # -- undersized: the machine actually stalled -------------------------

    if memory and memory.state is State.SATURATED:
        return _undersized(verdict, summary, memory, cat, pol, "memory")

    if cpu and cpu.state is State.SATURATED:
        return _undersized(verdict, summary, cpu, cat, pol, "cpu")

    if memory and memory.state is State.DEGRADED:
        verdict.sizing = Sizing.UNDERSIZED
        verdict.confidence = Confidence.MEDIUM
        verdict.headline = (
            f"Borderline. Memory stalled {memory.some_p95:.2f}% of the time "
            f"at p95 — above the {t.some_degraded:.1f}% comfort threshold "
            f"but below saturation."
        )
        verdict.reasons.append(
            "The workload fits, but with little room. One size up removes the "
            "stalls; staying put is defensible if the latency cost is acceptable."
        )
        if current:
            verdict.recommended = cat.step_up(current, 1) or current
        verdict.caveats.append(
            "Re-profile after any change: the verdict is only as good as the "
            "workload that was running during the measurement."
        )
        return verdict

    # -- healthy: is it too healthy? --------------------------------------

    return _healthy(verdict, summary, cat, pol)


def _undersized(
    verdict: Verdict,
    summary: RunSummary,
    resource,
    cat: Catalog,
    pol: Policy,
    kind: str,
) -> Verdict:
    verdict.sizing = Sizing.UNDERSIZED
    verdict.confidence = Confidence.HIGH

    stalled_pct = resource.some_p95
    lost = resource.stalled_seconds

    verdict.headline = (
        f"Undersized on {kind}. Stalled {stalled_pct:.2f}% of wall-clock time "
        f"at p95 ({lost:.1f}s lost to waiting across the run)."
    )

    # How far up to step. Two sizes only when there is hard evidence that the
    # deficit is large, because over-recommending is expensive.
    severe = (
        summary.oom_kills > 0
        or resource.full_max >= 5.0
        or summary.swap_in_total > 0
    )
    steps = 2 if severe else 1

    if kind == "memory":
        verdict.reasons.append(
            "Tasks were blocked waiting for memory. On this machine that means "
            "the working set does not fit in RAM, so the kernel is reclaiming "
            "pages that are still wanted."
        )
        if summary.direct_reclaim_per_s > 0:
            verdict.reasons.append(
                f"Direct reclaim ran at {summary.direct_reclaim_per_s:.0f} "
                "pages/s, meaning allocations had to free memory synchronously "
                "instead of letting kswapd work in the background."
            )
        if summary.oom_kills:
            verdict.reasons.append(
                f"The OOM killer fired {summary.oom_kills} time(s). Processes "
                "were terminated; this is not a tuning problem."
            )
    else:
        verdict.reasons.append(
            "Runnable tasks spent significant time waiting for a CPU. More "
            "vCPUs, not more RAM."
        )

    if verdict.current:
        target = cat.step_up(verdict.current, steps)
        if target:
            verdict.recommended = target
            verdict.reasons.append(
                f"Recommending {target.name} "
                f"({target.ram_gb:g} GB / {target.vcpu} vCPU), "
                f"{steps} size{'s' if steps > 1 else ''} up."
            )
        else:
            verdict.reasons.append(
                f"{verdict.current.name} is the largest size in its family. "
                "Move to a memory-optimised family (r-series) or scale out."
            )
    else:
        # No current type known, so recommend by absolute requirement instead.
        needed = verdict.peak_working_set_gb * (1 + pol.headroom)
        verdict.recommended = cat.smallest_with(ram_gb=needed)
        verdict.caveats.append(
            "No current instance type given, so the recommendation is derived "
            "from the observed working set rather than from a step up. Pass "
            "--instance-type for a family-aware answer."
        )

    verdict.caveats.append(
        "This is a floor, not a target: the run stalled, so the true working "
        "set may exceed what was observable. Re-profile after resizing."
    )
    return verdict


def _healthy(
    verdict: Verdict,
    summary: RunSummary,
    cat: Catalog,
    pol: Policy,
) -> Verdict:
    peak_ws = verdict.peak_working_set_gb
    needed_gb = peak_ws * (1 + pol.headroom)

    if summary.duration_s < pol.min_downsize_seconds:
        verdict.sizing = Sizing.RIGHT_SIZED
        verdict.confidence = Confidence.LOW
        verdict.headline = (
            "No saturation observed, but the run was too short to recommend "
            "shrinking."
        )
        verdict.reasons.append(
            f"Ran for {summary.duration_s:.0f}s; downsizing advice needs at "
            f"least {pol.min_downsize_seconds:.0f}s so that a realistic peak "
            "has a chance to appear."
        )
        verdict.recommended = verdict.current
        return verdict

    if not verdict.current:
        verdict.sizing = Sizing.RIGHT_SIZED
        verdict.confidence = Confidence.MEDIUM
        verdict.headline = (
            f"No saturation observed. Peak working set {peak_ws:.2f} GB."
        )
        verdict.recommended = cat.smallest_with(ram_gb=needed_gb)
        verdict.caveats.append(
            "Pass --instance-type to compare against what you are paying for."
        )
        return verdict

    candidate = cat.smallest_with(
        ram_gb=needed_gb,
        vcpu=1,
        family=verdict.current.family,
    )

    saving = 0.0
    if candidate:
        saving = verdict.current.usd_per_month - candidate.usd_per_month

    if candidate and candidate.ram_gb < verdict.current.ram_gb and saving >= pol.min_monthly_saving_usd:
        verdict.sizing = Sizing.OVERSIZED
        verdict.confidence = (
            Confidence.HIGH if summary.duration_s >= 1800 else Confidence.MEDIUM
        )
        verdict.recommended = candidate
        verdict.headline = (
            f"Oversized. Zero saturation across {summary.duration_s / 60:.0f} "
            f"minutes while holding {verdict.current.ram_gb:g} GB; peak working "
            f"set was {peak_ws:.2f} GB."
        )
        verdict.reasons.append(
            f"{candidate.name} provides {candidate.ram_gb:g} GB, which covers "
            f"the observed peak plus {pol.headroom:.0%} headroom "
            f"({needed_gb:.2f} GB)."
        )
        verdict.reasons.append(
            f"Estimated saving ${saving:.2f}/month per instance "
            f"(${saving * 12:.2f}/year)."
        )
        verdict.caveats.append(
            "Reference pricing, us-east-1 on-demand. Confirm against your own "
            "rates and any committed-use discounts before acting."
        )
        verdict.caveats.append(
            "The measurement only covers what ran during the window. Profile "
            "a peak period, not a quiet one, before shrinking production."
        )
        return verdict

    verdict.sizing = Sizing.RIGHT_SIZED
    verdict.confidence = Confidence.HIGH if summary.duration_s >= 1800 else Confidence.MEDIUM
    verdict.recommended = verdict.current
    verdict.headline = (
        f"Right-sized. No saturation, and the next size down would not leave "
        f"{pol.headroom:.0%} headroom above the {peak_ws:.2f} GB peak."
    )
    return verdict


def _build_evidence(summary: RunSummary, peak_ws: float) -> list[str]:
    """The numbers behind the verdict, in the order a reader needs them."""
    memory = summary.resources.get("memory")
    io = summary.resources.get("io")
    cpu = summary.resources.get("cpu")

    lines = [
        f"Observed for {summary.duration_s:.0f}s across {summary.samples} samples.",
    ]

    if memory:
        lines.append(
            f"Memory stall: p50 {memory.some_p50:.2f}%, p95 {memory.some_p95:.2f}%, "
            f"max {memory.some_max:.2f}%, full-stall max {memory.full_max:.2f}%."
        )
    if cpu:
        lines.append(f"CPU stall: p95 {cpu.some_p95:.2f}%, max {cpu.some_max:.2f}%.")
    if io:
        lines.append(f"IO stall: p95 {io.some_p95:.2f}%, max {io.some_max:.2f}%.")

    lines.append(
        f"Memory: {summary.mem_naive_used_pct_max:.1f}% by the used/free rule, "
        f"but the working set peaked at {peak_ws:.2f} GB of "
        f"{summary.mem_total_kb / (1024 * 1024):.2f} GB "
        f"({summary.mem_used_pct_max:.1f}%)."
    )
    lines.append(
        f"CPU utilisation: mean {summary.cpu_busy_pct_mean:.1f}%, "
        f"peak {summary.cpu_busy_pct_max:.1f}%."
    )

    if summary.direct_reclaim_total:
        lines.append(
            f"Direct reclaim: {summary.direct_reclaim_total} pages scanned "
            f"({summary.direct_reclaim_per_s:.0f}/s)."
        )
    if summary.major_faults_total:
        lines.append(f"Major page faults: {summary.major_faults_total}.")
    if summary.swap_in_total:
        lines.append(f"Pages swapped in: {summary.swap_in_total}.")
    if summary.oom_kills:
        lines.append(f"OOM kills: {summary.oom_kills}.")

    # The finding the whole project is built on.
    if memory:
        lines.append(
            f"Utilisation-vs-saturation gap: {summary.divergence:+.1f} points "
            f"({summary.mem_naive_used_pct_max:.1f}% \"used\", "
            f"{memory.some_p95:.2f}% stalled)."
        )

    return lines
