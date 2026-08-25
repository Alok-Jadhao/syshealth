"""Classification and summarisation.

Every threshold this project uses lives in this file, as a value on
``Thresholds``. Nothing anywhere else is allowed to hardcode a number, so that
"why did it say that?" always has exactly one place to look.

Thresholds are expressed as *percentage of wall-clock time stalled*, which is
a physical quantity that means the same thing on a Raspberry Pi and on a
24-core server. This is the reason the old baseline-calibration step is gone:
a ratio against an idle baseline is not comparable across machines, and on an
idle box with zero baseline it divides by a fudge constant.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum

from .models import Interval


class State(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    SATURATED = "SATURATED"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return {"HEALTHY": 0, "DEGRADED": 1, "SATURATED": 2, "UNKNOWN": -1}[self.value]


@dataclass(frozen=True)
class Thresholds:
    """Stall percentages that separate the three states.

    Defaults are deliberately conservative. A machine stalling 1% of the time
    is losing roughly 36 seconds an hour to waiting, which is already visible
    in tail latency; 10% is a machine in real trouble.

    ``full_*`` thresholds are lower because ``full`` means *nothing* ran: no
    task on the box made progress. A little ``some`` pressure is normal on a
    busy server. Sustained ``full`` pressure never is.
    """

    some_degraded: float = 1.0
    some_saturated: float = 10.0

    full_degraded: float = 0.5
    full_saturated: float = 2.0

    # Direct reclaim means an allocation had to free memory synchronously
    # rather than letting kswapd do it in the background. Sustained direct
    # reclaim is the signature of a working set that does not fit.
    direct_reclaim_per_s: float = 500.0

    # Percentile used for the headline number, so one unlucky spike does not
    # condemn an otherwise healthy machine.
    headline_percentile: int = 95

    # Utilisation exceeding saturation by more than this many percentage points
    # is the divergence this project exists to demonstrate, and is worth
    # pointing out explicitly to whoever — or whatever — is reading.
    divergence_note_pct: float = 25.0

    # A window with more background reclaim than this, or with any swap-in at
    # all, is not quiet enough to describe high utilisation as harmless. Well
    # below direct_reclaim_per_s: this is "is anything happening", not "is the
    # working set too big". Deliberately conservative, because the cost of
    # wrongly reassuring someone is higher than the cost of staying silent.
    quiet_reclaim_per_s: float = 10.0


def classify(
    sample: Interval,
    resource: str = "memory",
    thresholds: Thresholds | None = None,
) -> State:
    """Classify a single Interval for one resource."""
    t = thresholds or Thresholds()

    if not sample.psi_available:
        return State.UNKNOWN

    some = sample.some(resource)
    full = sample.full(resource)

    if some >= t.some_saturated or full >= t.full_saturated:
        return State.SATURATED
    if some >= t.some_degraded or full >= t.full_degraded:
        return State.DEGRADED
    return State.HEALTHY


@dataclass
class ResourceSummary:
    """What happened to one resource over a whole run."""

    resource: str
    some_p50: float = 0.0
    some_p95: float = 0.0
    some_max: float = 0.0
    full_max: float = 0.0
    stalled_seconds: float = 0.0
    state: State = State.UNKNOWN

    @property
    def headline_pct(self) -> float:
        return self.some_p95


@dataclass
class RunSummary:
    """Aggregate of a full measurement run. The input to every verdict."""

    duration_s: float = 0.0
    samples: int = 0
    psi_available: bool = True

    resources: dict[str, ResourceSummary] = field(default_factory=dict)

    mem_used_pct_mean: float = 0.0
    mem_used_pct_max: float = 0.0
    mem_naive_used_pct_max: float = 0.0
    mem_total_kb: int = 0
    swap_used_pct_max: float = 0.0
    cpu_busy_pct_mean: float = 0.0
    cpu_busy_pct_max: float = 0.0

    direct_reclaim_total: int = 0
    direct_reclaim_per_s: float = 0.0
    major_faults_total: int = 0
    swap_in_total: int = 0
    oom_kills: int = 0

    label: str = ""

    @property
    def state(self) -> State:
        """Worst state across all resources."""
        if not self.resources:
            return State.UNKNOWN
        return max(
            (r.state for r in self.resources.values()),
            key=lambda s: s.rank,
        )

    @property
    def bottleneck(self) -> str:
        """The resource that stalled most. Meaningless if state is HEALTHY."""
        if not self.resources:
            return "unknown"
        return max(self.resources, key=lambda r: self.resources[r].some_p95)

    @property
    def divergence(self) -> float:
        """How far utilisation is from saturation, in percentage points.

        The headline finding of the project. Measured against the *naive*
        utilisation figure, because that is the one on the dashboard that
        makes someone provision a bigger box. A large positive number means
        utilisation looked alarming while the machine was in fact fine.
        """
        mem = self.resources.get("memory")
        return self.mem_naive_used_pct_max - (mem.some_p95 if mem else 0.0)


def _pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    # Nearest-rank percentile: no interpolation, so the number reported is
    # always a value that was actually observed.
    index = min(int(round(p / 100.0 * len(ordered) + 0.5)) - 1, len(ordered) - 1)
    return ordered[max(index, 0)]


def summarise(
    samples: list[Interval],
    thresholds: Thresholds | None = None,
    label: str = "",
) -> RunSummary:
    """Fold a list of Intervals into one RunSummary."""
    t = thresholds or Thresholds()
    summary = RunSummary(label=label, samples=len(samples))

    if not samples:
        summary.psi_available = False
        return summary

    summary.duration_s = sum(s.duration_s for s in samples)
    summary.psi_available = all(s.psi_available for s in samples)

    for resource in ("memory", "io", "cpu"):
        some_values = [s.some(resource) for s in samples]
        full_values = [s.full(resource) for s in samples]
        stalled = sum(s.some(resource) / 100.0 * s.duration_s for s in samples)

        res = ResourceSummary(
            resource=resource,
            some_p50=_pct(some_values, 50),
            some_p95=_pct(some_values, t.headline_percentile),
            some_max=max(some_values),
            full_max=max(full_values) if full_values else 0.0,
            stalled_seconds=stalled,
        )

        # The run-level state uses the percentile, not the max, so a single
        # transient spike does not permanently condemn the machine.
        if not summary.psi_available:
            res.state = State.UNKNOWN
        elif res.some_p95 >= t.some_saturated or res.full_max >= t.full_saturated:
            res.state = State.SATURATED
        elif res.some_p95 >= t.some_degraded or res.full_max >= t.full_degraded:
            res.state = State.DEGRADED
        else:
            res.state = State.HEALTHY

        summary.resources[resource] = res

    mem_used = [s.mem_used_pct for s in samples]
    cpu_busy = [s.cpu_busy_pct for s in samples]

    summary.mem_used_pct_mean = statistics.fmean(mem_used)
    summary.mem_used_pct_max = max(mem_used)
    summary.mem_naive_used_pct_max = max(
        (s.mem_naive_used_pct for s in samples), default=0.0
    )
    summary.mem_total_kb = max(s.mem_total_kb for s in samples)
    summary.swap_used_pct_max = max(s.swap_used_pct for s in samples)
    summary.cpu_busy_pct_mean = statistics.fmean(cpu_busy)
    summary.cpu_busy_pct_max = max(cpu_busy)

    summary.direct_reclaim_total = sum(s.reclaim.get("pgscan_direct", 0) for s in samples)
    summary.major_faults_total = sum(s.reclaim.get("pgmajfault", 0) for s in samples)
    summary.swap_in_total = sum(s.reclaim.get("pswpin", 0) for s in samples)
    summary.oom_kills = sum(s.reclaim.get("oom_kill", 0) for s in samples)
    if summary.duration_s > 0:
        summary.direct_reclaim_per_s = summary.direct_reclaim_total / summary.duration_s

    return summary
