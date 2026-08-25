"""Deciding that something is wrong.

Detection reads the same fleet telemetry everything else does and asks one
question per node: is this bad enough to open an incident about?

Two properties matter more than the thresholds themselves.

**Deduplication.** A detector that runs every few seconds against an ongoing
problem must open one incident, not four hundred. Every finding carries a
fingerprint — node plus symptom — and an open incident with the same
fingerprint suppresses a new one.

**Hysteresis.** A node oscillating either side of a threshold must not open and
close incidents on every pass. Detection requires the condition to hold across
consecutive observations before it fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..analysis import State, Thresholds
from .incidents import Severity


@dataclass(frozen=True)
class Finding:
    """One reason to open an incident."""

    node: str
    severity: Severity
    title: str
    fingerprint: str
    symptom: str
    detail: dict[str, Any]


# How many consecutive observations must agree before an incident opens.
CONSECUTIVE = 2


class Detector:
    """Turns fleet health readings into findings.

    Stateful across calls, because hysteresis needs memory of the last pass.
    """

    def __init__(self, thresholds: Thresholds | None = None, consecutive: int = CONSECUTIVE) -> None:
        self.t = thresholds or Thresholds()
        self.consecutive = max(1, consecutive)
        self._streaks: dict[str, int] = {}

    def examine(self, health: dict[str, Any]) -> Finding | None:
        """Look at one ``get_node_health`` payload and decide."""
        node = health.get("node", "?")
        candidate = self._symptom(node, health)

        if candidate is None:
            # Clear every streak for this node: whatever was building has
            # stopped, and a half-formed streak must not survive to combine
            # with a later unrelated blip.
            for key in [k for k in self._streaks if k.startswith(f"{node}:")]:
                del self._streaks[key]
            return None

        streak = self._streaks.get(candidate.fingerprint, 0) + 1
        self._streaks[candidate.fingerprint] = streak

        if streak < self.consecutive:
            return None
        return candidate

    def _symptom(self, node: str, health: dict[str, Any]) -> Finding | None:
        resources = health.get("resources", {})
        reclaim = health.get("reclaim", {})
        state = State(health.get("state", State.UNKNOWN.value))

        # An unmeasurable node is worth knowing about, but it is an
        # observability failure rather than a saturation incident, and it must
        # never be remediated — there is no evidence to remediate from.
        if not health.get("psi_available", True):
            return Finding(
                node=node,
                severity=Severity.WARNING,
                title=f"{node}: saturation cannot be measured (no PSI)",
                fingerprint=f"{node}:no-psi",
                symptom="no-psi",
                detail={"psi_available": False},
            )

        oom = int(reclaim.get("oom_kills", 0))
        if oom > 0:
            return Finding(
                node=node,
                severity=Severity.CRITICAL,
                title=f"{node}: {oom} OOM kill(s) — the kernel killed a process",
                fingerprint=f"{node}:oom",
                symptom="oom",
                detail={"oom_kills": oom},
            )

        # Every saturated resource, worst first. Taking the first in a fixed
        # order instead would title an incident "memory saturated" on a node
        # stalling 2.9% on memory and 52% on io, and the investigation would
        # then contradict the incident it was opened for.
        saturated = [
            (resource, summary)
            for resource in ("memory", "io", "cpu")
            for summary in [resources.get(resource, {})]
            if float(summary.get("some_p95_pct", 0.0)) >= self.t.some_saturated
            or float(summary.get("full_max_pct", 0.0)) >= self.t.full_saturated
        ]
        if saturated:
            resource, summary = max(
                saturated,
                key=lambda pair: (
                    float(pair[1].get("some_p95_pct", 0.0)),
                    float(pair[1].get("full_max_pct", 0.0)),
                ),
            )
            p95 = float(summary.get("some_p95_pct", 0.0))
            full = float(summary.get("full_max_pct", 0.0))
            also = [r for r, _ in saturated if r != resource]
            return Finding(
                node=node,
                severity=Severity.CRITICAL,
                title=(
                    f"{node}: {resource} saturated — tasks stalled "
                    f"{p95:.1f}% of wall-clock time at p95"
                    + (f" (also {', '.join(also)})" if also else "")
                ),
                fingerprint=f"{node}:{resource}-saturated",
                symptom=f"{resource}-saturated",
                detail={
                    "resource": resource,
                    "some_p95_pct": p95,
                    "full_max_pct": full,
                    "also_saturated": also,
                },
            )

        if state is State.DEGRADED:
            bottleneck = health.get("bottleneck") or "unknown"
            summary = resources.get(bottleneck, {})
            return Finding(
                node=node,
                severity=Severity.WARNING,
                title=(
                    f"{node}: {bottleneck} degraded — stalled "
                    f"{float(summary.get('some_p95_pct', 0.0)):.1f}% at p95"
                ),
                fingerprint=f"{node}:{bottleneck}-degraded",
                symptom=f"{bottleneck}-degraded",
                detail={"resource": bottleneck, **summary},
            )

        return None

    def sweep(self, healths: list[dict[str, Any]]) -> list[Finding]:
        """Examine every node, worst first."""
        findings = [f for f in (self.examine(h) for h in healths) if f is not None]
        return sorted(findings, key=lambda f: f.severity.rank, reverse=True)
