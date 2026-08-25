"""The policy engine: what is allowed to happen, and who has to say so.

This module is the authority. Everything else asks it. It is deliberately
boring, entirely deterministic, and has no dependency on the reasoner, the
tools, or the network — so a reviewer can read it end to end and know what the
system can do without tracing anything.

The central rule: **a reasoner's confidence cannot widen its permissions.**
"High confidence" is not an argument the policy engine accepts, because the
whole premise is that a generated diagnosis may be wrong. What a hypothesis
can do is select an action from the catalogue; whether that action may run is
decided here, from the tier, the mode, and the guards.

The guards exist because the dangerous failure of an autonomous system is not
one wrong action. It is the same wrong action, forever, faster than anyone can
intervene.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from ..mcp.tools import Tier


class Mode(str, Enum):
    """How much the system may do without a human.

    Configurable, and deliberately defaulting to the most restrictive.
    """

    OBSERVE = "OBSERVE"
    """Investigate and recommend. Nothing may change."""

    ASSIST = "ASSIST"
    """Investigate, plan, and ask. Every change needs a human yes."""

    AUTONOMOUS = "AUTONOMOUS"
    """Low-risk actions may run unattended. High-risk still needs a human."""


class Decision(str, Enum):
    ALLOW = "ALLOW"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    DENY = "DENY"


class PolicyError(RuntimeError):
    """Raised when something tries to execute what policy did not permit."""


@dataclass(frozen=True)
class Ruling:
    """A decision, and the reason for it, in the words used in the audit log."""

    decision: Decision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass
class Policy:
    """Configuration for what may happen, and how often.

    Defaults are the safe end of every axis: observe only, two attempts, one
    node at a time. Loosening any of them should be a deliberate act recorded
    in a config file, not something that happens because nobody set it.
    """

    mode: Mode = Mode.OBSERVE

    # Which LOW_RISK actions may run unattended in AUTONOMOUS mode. Empty by
    # default: turning autonomy on is one decision, and choosing what it may
    # do is a second, separate one. An action absent from this set falls back
    # to asking a human rather than being refused, so tightening the list
    # degrades to ASSIST rather than to a dead system.
    autonomous_actions: frozenset[str] = frozenset()

    # The single most important guard. An incident that has already been acted
    # on twice without recovering is not going to be fixed by a third
    # identical attempt; it needs a person.
    max_attempts_per_incident: int = 2

    # Never run the same action on the same node more often than this,
    # regardless of how many incidents ask for it. Two incidents opening on
    # correlated symptoms must not become a restart loop.
    cooldown_s: float = 300.0

    # How many nodes may be under active remediation at once. A fleet-wide
    # symptom — a bad deploy, say — must not become a fleet-wide restart.
    max_concurrent_nodes: int = 1

    # An incident open longer than this is escalated rather than worked on
    # forever.
    incident_timeout_s: float = 1800.0

    # How long a single action may run on a node before it is abandoned.
    action_timeout_s: float = 120.0

    # An approval nobody answered must not execute hours later against a
    # machine whose state has moved on.
    approval_ttl_s: float = 900.0

    # Populated at runtime; not configuration.
    _recent: dict[tuple[str, str], float] = field(default_factory=dict, repr=False)

    # -- the ruling ---------------------------------------------------------

    def rule(
        self,
        tier: Tier,
        action: str,
        node: str,
        attempts: int = 0,
        active_nodes: int = 0,
        now: float | None = None,
    ) -> Ruling:
        """Decide whether one action may run, right now, on one node."""
        now = now if now is not None else time.time()

        if tier is Tier.READ_ONLY:
            return Ruling(Decision.ALLOW, "read-only tools are always permitted")

        if self.mode is Mode.OBSERVE:
            return Ruling(
                Decision.DENY,
                f"mode is OBSERVE: {action!r} would change the machine, and "
                "this mode may only investigate and recommend",
            )

        # Guards apply before the tier does. A HIGH_RISK action that would
        # breach a guard should be refused outright rather than put in front
        # of a human, because the guard is the thing protecting them from
        # approving the fourth restart in five minutes.
        if attempts >= self.max_attempts_per_incident:
            return Ruling(
                Decision.DENY,
                f"this incident has already been acted on {attempts} time(s), "
                f"the limit is {self.max_attempts_per_incident}. Remediation is "
                "not working; escalate to a human rather than trying again",
            )

        last = self._recent.get((node, action))
        if last is not None and now - last < self.cooldown_s:
            remaining = self.cooldown_s - (now - last)
            return Ruling(
                Decision.DENY,
                f"{action!r} ran on {node} {now - last:.0f}s ago and is in "
                f"cooldown for another {remaining:.0f}s",
            )

        if active_nodes >= self.max_concurrent_nodes:
            return Ruling(
                Decision.DENY,
                f"{active_nodes} node(s) already under remediation, limit is "
                f"{self.max_concurrent_nodes}. A correlated fleet-wide symptom "
                "must not become a fleet-wide action",
            )

        if tier is Tier.HIGH_RISK:
            return Ruling(
                Decision.NEEDS_APPROVAL,
                f"{action!r} is HIGH_RISK: destructive or irreversible actions "
                "require a human decision in every mode, including AUTONOMOUS",
            )

        if self.mode is Mode.ASSIST:
            return Ruling(
                Decision.NEEDS_APPROVAL,
                f"mode is ASSIST: {action!r} is proposed for human approval",
            )

        if action not in self.autonomous_actions:
            return Ruling(
                Decision.NEEDS_APPROVAL,
                f"mode is AUTONOMOUS but {action!r} is not in the autonomous "
                "action list, so it needs approval",
            )

        return Ruling(
            Decision.ALLOW,
            f"mode is AUTONOMOUS and {action!r} is a permitted low-risk action",
        )

    # -- bookkeeping --------------------------------------------------------

    def record_execution(self, node: str, action: str, now: float | None = None) -> None:
        """Start the cooldown. Called when an action is dispatched, not when
        it succeeds: a failed restart still perturbed the machine."""
        self._recent[(node, action)] = now if now is not None else time.time()

    def approval_expired(self, approved_at: float, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now - approved_at > self.approval_ttl_s

    def incident_expired(self, opened_at: float, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now - opened_at > self.incident_timeout_s

    # -- construction -------------------------------------------------------

    @classmethod
    def from_settings(cls, settings) -> Policy:
        """Build from ``Settings``, which is the only place env/config is read."""
        actions = settings.autonomous_actions
        if isinstance(actions, str):
            actions = [a.strip() for a in actions.split(",") if a.strip()]

        try:
            mode = Mode(str(settings.mode).strip().upper())
        except ValueError as exc:
            valid = ", ".join(m.value for m in Mode)
            raise ValueError(f"mode must be one of {valid}; got {settings.mode!r}") from exc

        return cls(
            mode=mode,
            autonomous_actions=frozenset(actions or ()),
            max_attempts_per_incident=int(settings.max_attempts),
            cooldown_s=float(settings.cooldown_s),
            max_concurrent_nodes=int(settings.max_concurrent_nodes),
            incident_timeout_s=float(settings.incident_timeout_s),
        )

    def describe(self) -> dict:
        """For the audit log and the dashboard: what was in force at the time."""
        return {
            "mode": self.mode.value,
            "autonomous_actions": sorted(self.autonomous_actions),
            "max_attempts_per_incident": self.max_attempts_per_incident,
            "cooldown_s": self.cooldown_s,
            "max_concurrent_nodes": self.max_concurrent_nodes,
            "incident_timeout_s": self.incident_timeout_s,
            "action_timeout_s": self.action_timeout_s,
            "approval_ttl_s": self.approval_ttl_s,
        }
