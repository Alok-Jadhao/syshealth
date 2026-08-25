"""The autonomous SRE layer.

SysHealth measures. This package decides what to do about what was measured,
and — within stated boundaries — does it.

The shape of the thing:

    detect      an abnormal condition in the telemetry opens an incident
    investigate read-only tools gather evidence
    diagnose    a reasoner proposes a cause, citing that evidence
    plan        a remediation is chosen from a fixed catalogue
    authorise   the policy engine decides: allow, ask a human, or refuse
    execute     the node runs it from an allowlist, never an arbitrary string
    verify      the machine is measured again; success is recovery, not exit 0
    close       or escalate, having recorded every step

Three rules run through all of it.

**The reasoner is not trusted.** It is one component behind an interface, and
a deterministic rule-based one is the default. Nothing it says can widen what
the policy engine permits, and every claim it makes must cite evidence that
was actually retrieved. Swapping in an LLM changes the quality of the
hypothesis, not the authority of the system.

**Nothing executes that was not named in advance.** Actions come from a
registry with typed arguments and a permission tier. There is no shell tool,
no "run this command", and no path by which a reasoner can invent one.

**Everything is written down before it happens.** An action is a durable,
audited row in the database before any node can see it, and its result is
recorded whether it worked or not. "Why did it restart that container?" has
an answer that does not depend on anyone's memory.
"""

from __future__ import annotations

from .actions import ACTIONS, Action, ActionSpec
from .incidents import Incident, IncidentStore, Severity, Status, TimelineEvent
from .policy import Decision, Mode, Policy, PolicyError
from .verify import VerificationResult, verify_recovery

__all__ = [
    "ACTIONS",
    "Action",
    "ActionSpec",
    "Decision",
    "Incident",
    "IncidentStore",
    "Mode",
    "Policy",
    "PolicyError",
    "Severity",
    "Status",
    "TimelineEvent",
    "VerificationResult",
    "verify_recovery",
]
