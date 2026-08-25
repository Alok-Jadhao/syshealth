"""Did it actually work?

The rule this module exists to enforce: **an action reporting success is not
an incident resolving.** ``restart_container`` returning exit 0 means a
container restarted. It does not mean the memory leak is gone, the latency
recovered, or the pressure subsided — and on a bad day it means the container
restarted and immediately began leaking again.

So verification never reads the action's result. It re-measures the machine
and checks the conditions the incident was opened on, plus the condition the
action claimed it would restore. Recovery is a measurement, not a return code.

Two subtleties that are easy to get wrong:

**Wait before measuring.** A machine sampled the instant a service restarts is
not yet the machine you will have in thirty seconds. PSI is a decaying
counter; measuring immediately reads the stall that happened *before* the fix.

**Failing to recover is information, not an error.** A verification that finds
the problem still present has worked correctly. It returns ``recovered=False``
and the loop escalates. Nothing here raises on an unrecovered incident.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..analysis import State, Thresholds


@dataclass
class Check:
    """One thing that had to become true again."""

    name: str
    passed: bool
    observed: str
    expected: str

    def describe(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}: {self.observed}"


@dataclass
class VerificationResult:
    recovered: bool
    summary: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovered": self.recovered,
            "summary": self.summary,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "observed": c.observed,
                    "expected": c.expected,
                }
                for c in self.checks
            ],
        }


# How long to let a machine settle before believing what it says. A restart
# that has not finished starting is not evidence of anything.
SETTLE_S = 20.0

# How much of the original stall must be gone to call it recovery. Requiring
# a return to zero would fail on any busy-but-healthy machine; requiring only
# "less than before" would accept noise.
RECOVERY_FRACTION = 0.5


def verify_recovery(
    health: dict[str, Any],
    baseline: dict[str, Any],
    resource: str = "memory",
    thresholds: Thresholds | None = None,
    expectation: str = "",
) -> VerificationResult:
    """Compare a post-remediation reading against the one that opened the incident.

    ``health`` and ``baseline`` are ``get_node_health`` payloads. Deliberately
    plain dicts rather than a live measurement call: verification is then a
    pure function, testable against recorded data, and the loop owns when and
    how the reading is taken.
    """
    t = thresholds or Thresholds()
    checks: list[Check] = []

    before = baseline.get("resources", {}).get(resource, {})
    after = health.get("resources", {}).get(resource, {})

    before_p95 = float(before.get("some_p95_pct", 0.0))
    after_p95 = float(after.get("some_p95_pct", 0.0))

    # 1. Is the machine measurable at all? An unmeasurable machine has not
    #    been shown to have recovered; it has been shown to be unreadable.
    measurable = bool(health.get("psi_available", False))
    checks.append(
        Check(
            name="machine is measurable",
            passed=measurable,
            observed=f"psi_available={measurable}",
            expected="PSI readable, so saturation can be observed",
        )
    )

    # 2. Is the node still reporting? A node that went silent after a restart
    #    is a worse outcome than the incident, not a resolved one.
    online = bool(health.get("online", False))
    checks.append(
        Check(
            name="node is still reporting",
            passed=online,
            observed=f"online={online}, last sample "
            f"{health.get('seconds_since_last_sample', '?')}s ago",
            expected="the node kept pushing telemetry through the remediation",
        )
    )

    # 3. Has the state itself come down?
    after_state = State(health.get("state", State.UNKNOWN.value))
    before_state = State(baseline.get("state", State.UNKNOWN.value))
    improved = after_state.rank < before_state.rank or after_state is State.HEALTHY
    checks.append(
        Check(
            name="health state improved",
            passed=improved,
            observed=f"{before_state.value} -> {after_state.value}",
            expected="a state better than the one the incident opened on",
        )
    )

    # 4. Has the stall that caused the incident actually fallen? This is the
    #    check that a successful-but-useless remediation fails.
    target = min(before_p95 * RECOVERY_FRACTION, t.some_degraded)
    fell = after_p95 <= target
    checks.append(
        Check(
            name=f"{resource} stall fell",
            passed=fell,
            observed=f"p95 {before_p95:.2f}% -> {after_p95:.2f}%",
            expected=f"p95 at or below {target:.2f}%",
        )
    )

    # 5. Has the kernel stopped struggling? Pressure can lag; OOM kills and
    #    swap-in during the *post* window are unambiguous.
    reclaim = health.get("reclaim", {})
    oom = int(reclaim.get("oom_kills", 0))
    swap_in = int(reclaim.get("swap_in_total", 0))
    quiet = oom == 0 and swap_in == 0
    checks.append(
        Check(
            name="kernel is no longer under memory duress",
            passed=quiet,
            observed=f"oom_kills={oom}, swap_in={swap_in} since remediation",
            expected="no OOM kills and no swap-in after the fix",
        )
    )

    if expectation:
        checks.append(
            Check(
                name="action's stated expectation",
                passed=fell and improved,
                observed=f"{resource} p95 {after_p95:.2f}%, state {after_state.value}",
                expected=expectation,
            )
        )

    recovered = all(c.passed for c in checks)
    if recovered:
        summary = (
            f"recovered: {resource} stall p95 {before_p95:.2f}% -> {after_p95:.2f}%, "
            f"state {before_state.value} -> {after_state.value}, kernel quiet"
        )
    else:
        names = ", ".join(c.name for c in checks if not c.passed)
        summary = f"not recovered: {names}"

    return VerificationResult(recovered=recovered, summary=summary, checks=checks)


def settle(seconds: float = SETTLE_S, sleep=time.sleep) -> None:
    """Let the machine become what it is going to be before measuring it."""
    sleep(max(0.0, seconds))
