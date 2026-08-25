"""The tools themselves. No MCP SDK imports live in this file.

Two things here are load-bearing beyond "wrap a function".

**Every tool declares a permission tier.** Phase 1 is entirely
``READ_ONLY``, so the tier does nothing yet except become MCP's
``read_only_hint``. It exists now because a policy layer bolted onto tools
that were written without one is how an agent ends up with more authority
than anyone decided to give it.

**Every metric tool reports saturation and utilisation together.** A tool
schema is a prompt: it is the only description of the data an agent ever
reads. Handing a model ``memory: 94%`` invites it to diagnose a memory
problem on a healthy cache-heavy box — which is precisely the false alarm
this project exists to refute. So the descriptions below say, in the text the
model actually sees, which number is evidence and which one lies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..analysis import Thresholds, classify
from ..models import Interval
from .sources import MetricSource

# Bounds on the measurement window a caller may request. A tool that blocks
# for an unbounded time on request is a denial-of-service primitive, and one
# that measures for 50ms reports noise as a verdict.
MIN_WINDOW_S = 0.2
MAX_WINDOW_S = 30.0
DEFAULT_WINDOW_S = 2.0


class Tier(str, Enum):
    """What a tool is allowed to do, and therefore who must approve it.

    ``READ_ONLY`` observes and cannot change anything.
    ``LOW_RISK`` changes state recoverably — restart a container, clear a
    temp file — and may be automatic depending on execution mode.
    ``HIGH_RISK`` is destructive or irreversible and always needs a human.
    """

    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    HIGH_RISK = "HIGH_RISK"

    @property
    def read_only(self) -> bool:
        return self is Tier.READ_ONLY

    @property
    def destructive(self) -> bool:
        return self is Tier.HIGH_RISK


@dataclass(frozen=True)
class Tool:
    """One callable capability, with the metadata a policy layer will need."""

    name: str
    tier: Tier
    description: str
    handler: Callable[..., dict[str, Any]]


class ToolInputError(ValueError):
    """An argument this layer rejected, with a message safe to return.

    The distinction matters at the protocol boundary: an anticipated
    rejection should reach the caller as an explanation it can act on, while
    an unexpected crash should not leak its internals. ``server.py`` maps this
    onto the SDK's equivalent. It stays a ``ValueError`` so callers that use
    the tools in-process — tests today, the agent later — need not know the
    MCP SDK exists.
    """


def _window(window_s: float) -> float:
    """Validate a requested window rather than silently clamping it.

    Clamping would make ``measured_s`` disagree with what the caller asked
    for without saying so. An explicit error teaches the bound instead — but
    only if the message survives the trip back, which is what
    ``ToolInputError`` is for.
    """
    if isinstance(window_s, bool) or not isinstance(window_s, (int, float)):
        raise ToolInputError(f"window_s must be a number, got {type(window_s).__name__}")
    if not MIN_WINDOW_S <= window_s <= MAX_WINDOW_S:
        raise ToolInputError(
            f"window_s must be between {MIN_WINDOW_S} and {MAX_WINDOW_S} seconds, "
            f"got {window_s}"
        )
    return float(window_s)


def _r(value: float, places: int = 2) -> float:
    return round(float(value), places)


def _quiet_high_utilisation(sample: Interval, t: Thresholds) -> bool:
    """Whether this window is the cache-heavy false alarm, and only that.

    Requires all four: a big utilisation-over-saturation gap, no meaningful
    ``some`` stall, no ``full`` stall, and a quiet kernel. The last condition
    is what keeps an early calm window in a thrashing run from being described
    as harmless — PSI can read zero for a window in which the machine was
    already reclaiming hard and swapping pages back in.
    """
    some = sample.some("memory")
    duration = max(sample.duration_s, 1e-6)
    direct_per_s = sample.reclaim.get("pgscan_direct", 0) / duration

    return (
        sample.mem_naive_used_pct - some >= t.divergence_note_pct
        and some < t.some_degraded
        and sample.full("memory") < t.full_degraded
        and direct_per_s < t.quiet_reclaim_per_s
        and sample.reclaim.get("pswpin", 0) == 0
    )


def _envelope(source: MetricSource, sample: Interval) -> dict[str, Any]:
    """Fields every tool response carries.

    ``source`` is not decoration. It is how a reader — human or model — can
    tell a live machine from a replayed recording.
    """
    return {
        "source": source.name,
        "measured_s": _r(sample.duration_s, 3),
        "psi_available": sample.psi_available,
    }


def build_tools(source: MetricSource, thresholds: Thresholds | None = None) -> dict[str, Tool]:
    """Bind the read-only tool set to one measurement source."""
    t = thresholds or Thresholds()

    # ------------------------------------------------------------- health --

    def get_health(window_s: float = DEFAULT_WINDOW_S) -> dict[str, Any]:
        sample = source.measure(_window(window_s))
        result = _envelope(source, sample)

        resources = {}
        for resource in ("memory", "cpu", "io"):
            resources[resource] = {
                "state": classify(sample, resource, t).value,
                "some_stall_pct": _r(sample.some(resource)),
                "full_stall_pct": _r(sample.full(resource)),
            }

        worst = max(
            resources,
            key=lambda r: (classify(sample, r, t).rank, resources[r]["some_stall_pct"]),
        )
        result["resources"] = resources
        result["state"] = classify(sample, worst, t).value
        result["bottleneck"] = worst if resources[worst]["some_stall_pct"] > 0 else None

        if not sample.psi_available:
            result["caveat"] = (
                "This kernel does not expose /proc/pressure, so no saturation was "
                "measured. State is UNKNOWN, not healthy. Do not infer health from "
                "the absence of stalling here."
            )
        return result

    # ------------------------------------------------------------- memory --

    def get_memory_pressure(window_s: float = DEFAULT_WINDOW_S) -> dict[str, Any]:
        sample = source.measure(_window(window_s))
        result = _envelope(source, sample)

        some = sample.some("memory")
        result["saturation"] = {
            "state": classify(sample, "memory", t).value,
            "some_stall_pct": _r(some),
            "full_stall_pct": _r(sample.full("memory")),
            "stalled_seconds": _r(some / 100.0 * sample.duration_s, 3),
        }
        result["utilisation"] = {
            "working_set_pct": _r(sample.mem_used_pct),
            "naive_used_pct": _r(sample.mem_naive_used_pct),
            "swap_used_pct": _r(sample.swap_used_pct),
            "total_gb": _r(sample.mem_total_kb / (1024 * 1024), 3),
        }

        divergence = sample.mem_naive_used_pct - some
        result["divergence_pct_points"] = _r(divergence)
        if _quiet_high_utilisation(sample, t):
            # Scoped to this window on purpose. An early quiet window in a run
            # that is otherwise thrashing will land here, and prose asserting
            # the machine is "healthy" would be manufactured reassurance about
            # a box in real trouble.
            result["divergence_note"] = (
                f"In this window naive_used_pct was {_r(sample.mem_naive_used_pct)}% "
                f"while memory stalled {_r(some)}% of the wall clock, with no "
                "reclaim or swap-in. Page cache counts toward naive_used_pct, so "
                "that figure alone is not evidence of a memory problem. This "
                "describes one window: check get_reclaim_activity and further "
                "windows before concluding the machine is healthy."
            )
        return result

    # ---------------------------------------------------------------- cpu --

    def get_cpu_pressure(window_s: float = DEFAULT_WINDOW_S) -> dict[str, Any]:
        sample = source.measure(_window(window_s))
        result = _envelope(source, sample)
        result["saturation"] = {
            "state": classify(sample, "cpu", t).value,
            "some_stall_pct": _r(sample.some("cpu")),
        }
        result["utilisation"] = {"busy_pct": _r(sample.cpu_busy_pct)}
        result["note"] = (
            "busy_pct is how much of the CPU was occupied; some_stall_pct is how "
            "much of the wall clock runnable tasks spent waiting for it. Only the "
            "second one means work was lost. The kernel does not report a "
            "meaningful 'full' share for CPU."
        )
        return result

    # ------------------------------------------------------------ reclaim --

    def get_reclaim_activity(window_s: float = DEFAULT_WINDOW_S) -> dict[str, Any]:
        sample = source.measure(_window(window_s))
        result = _envelope(source, sample)

        duration = max(sample.duration_s, 1e-6)
        counts = {name: int(sample.reclaim.get(name, 0)) for name in sample.reclaim}
        direct_per_s = counts.get("pgscan_direct", 0) / duration

        result["counts"] = counts
        result["direct_reclaim_per_s"] = _r(direct_per_s)
        result["sustained_direct_reclaim"] = direct_per_s >= t.direct_reclaim_per_s
        result["oom_kills"] = counts.get("oom_kill", 0)
        result["note"] = (
            "Direct reclaim means an allocation had to free memory synchronously "
            "instead of letting kswapd do it in the background; sustained direct "
            f"reclaim (>= {t.direct_reclaim_per_s}/s) is the signature of a working "
            "set that does not fit. Any oom_kill or pswpin above zero is hard "
            "evidence of memory exhaustion during the window."
        )
        return result

    definitions = (
        (
            get_health,
            "get_health",
            "Overall saturation state of the instance for one measured window: "
            "HEALTHY, DEGRADED, SATURATED, or UNKNOWN, per resource, plus which "
            "resource is the bottleneck. Start here, then use the per-resource "
            "tools for evidence. States come from the share of wall-clock time "
            "tasks spent stalled, not from how full anything is.",
        ),
        (
            get_memory_pressure,
            "get_memory_pressure",
            "Memory saturation and utilisation side by side for one measured "
            "window. Diagnose from `saturation` — the share of wall-clock time "
            "tasks spent blocked on memory. Do NOT diagnose from `utilisation`: "
            "Linux fills spare RAM with page cache, so a perfectly healthy machine "
            "routinely reads 90%+ used while stalling zero, and a thrashing one can "
            "read far less. High utilisation with near-zero stall needs no action.",
        ),
        (
            get_cpu_pressure,
            "get_cpu_pressure",
            "CPU saturation (time runnable tasks spent waiting for a core) next to "
            "CPU utilisation (time cores were busy) for one measured window. A box "
            "at 100% busy with no stalling is fully used, not overloaded.",
        ),
        (
            get_reclaim_activity,
            "get_reclaim_activity",
            "Kernel memory-reclaim counters over one measured window: direct "
            "reclaim rate, kswapd activity, major faults, swap in/out, and OOM "
            "kills. Corroborating evidence for a memory diagnosis — these are "
            "physical events, so a non-zero oom_kill or swap-in confirms what "
            "pressure alone can only suggest.",
        ),
    )

    return {
        name: Tool(name=name, tier=Tier.READ_ONLY, description=description, handler=fn)
        for fn, name, description in definitions
    }
