"""The incident loop: detect, investigate, diagnose, authorise, act, verify.

This is the orchestrator, and it owns exactly one thing — the order in which
the other components are consulted, and what happens when one of them says no.
It contains no thresholds, no analysis, and no judgement of its own; every
decision it makes is delegated to the module that owns that decision.

The loop that matters:

    finding -> incident -> evidence -> diagnosis -> ruling
                                                      |
                              +-----------+-----------+
                              |           |           |
                            DENY   NEEDS_APPROVAL   ALLOW
                              |           |           |
                          escalate      queue       dispatch
                                          |           |
                                          +-----> verify
                                                      |
                                            recovered / not
                                                      |
                                          resolve / try again / escalate

Termination is the property to check when reading this. Every path out of a
remediation attempt either resolves the incident, escalates it, or increments
an attempt counter that the policy engine bounds. There is no edge that
returns to remediation without passing through that counter, which is what
makes an infinite autonomous loop impossible rather than unlikely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..analysis import Thresholds
from ..mcp.tools import Tool
from .actions import Action
from .detect import Detector, Finding
from .incidents import ActionStatus, IncidentStore, Status
from .policy import Decision, Policy
from .reason import Diagnosis, DiagnosisError, InvestigationContext, Reasoner
from .verify import VerificationResult, verify_recovery


@dataclass
class LoopResult:
    """What one pass over one incident did. Returned for logging and tests."""

    incident_id: str
    status: Status
    diagnosis: Diagnosis | None = None
    action: Action | None = None
    action_id: int | None = None
    decision: Decision | None = None
    verification: VerificationResult | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "status": self.status.value,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "action": self.action.describe() if self.action else None,
            "action_id": self.action_id,
            "decision": self.decision.value if self.decision else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "note": self.note,
        }


class IncidentLoop:
    """Drives incidents from detection to resolution.

    Not a daemon. Every method is a single pass, so the caller owns the clock
    and the tests do not have to fight one.
    """

    def __init__(
        self,
        tools: dict[str, Tool],
        store: IncidentStore,
        policy: Policy,
        reasoner: Reasoner,
        detector: Detector | None = None,
        thresholds: Thresholds | None = None,
        managed: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.tools = tools
        self.store = store
        self.policy = policy
        self.reasoner = reasoner
        self.detector = detector or Detector(thresholds)
        self.t = thresholds or Thresholds()
        self.managed = managed or {}

    # -- detection ----------------------------------------------------------

    def detect(self) -> list[str]:
        """One sweep of the fleet. Returns the ids of incidents opened."""
        healths = []
        for node in self._nodes():
            try:
                healths.append(self.tools["get_node_health"].handler(node=node))
            except Exception:  # noqa: BLE001 - one unreadable node must not stop the sweep
                continue

        opened = []
        for finding in self.detector.sweep(healths):
            incident_id = self.open(finding)
            if incident_id:
                opened.append(incident_id)
        return opened

    def open(self, finding: Finding) -> str | None:
        """Open an incident, unless one is already open for the same symptom."""
        existing = self.store.find_open(finding.fingerprint)
        if existing is not None:
            self.store.add_event(
                existing.id,
                "detected",
                "symptom still present",
                finding.detail,
            )
            return None

        incident = self.store.open_incident(
            node=finding.node,
            severity=finding.severity,
            title=finding.title,
            mode=self.policy.mode.value,
            fingerprint=finding.fingerprint,
        )
        return incident.id

    # -- one pass over one incident -----------------------------------------

    def advance(self, incident_id: str) -> LoopResult:
        """Move one incident forward by one step."""
        incident = self.store.get(incident_id, with_timeline=False)
        if incident is None:
            raise KeyError(f"no such incident: {incident_id}")
        if incident.status.terminal:
            return LoopResult(incident_id, incident.status, note="already closed")

        if self.policy.incident_expired(incident.opened_ts):
            return self._escalate(
                incident_id,
                f"open for {incident.age_s:.0f}s without resolving, past the "
                f"{self.policy.incident_timeout_s:.0f}s limit",
            )

        if incident.status is Status.AWAITING_APPROVAL:
            return self._check_approval(incident_id)

        if incident.status is Status.VERIFYING:
            return self._verify(incident_id)

        # OPEN / INVESTIGATING / DIAGNOSED / REMEDIATING -> investigate afresh.
        return self._investigate(incident_id)

    # -- investigation and diagnosis ----------------------------------------

    def _investigate(self, incident_id: str) -> LoopResult:
        incident = self.store.get(incident_id, with_timeline=False)
        self.store.set_status(
            incident_id, Status.INVESTIGATING, f"{self.reasoner.name} reasoner investigating"
        )

        context = InvestigationContext(
            incident_id=incident_id,
            node=incident.node,
            title=incident.title,
            tools=self.tools,
            record=self.store,
            managed=self.managed.get(incident.node, {}),
        )

        try:
            diagnosis = self.reasoner.investigate(context)
            diagnosis.validate(context.collected)
        except DiagnosisError as exc:
            # A reasoner that fails its own contract does not get to be
            # ignored quietly. The incident goes to a human with the reason.
            return self._escalate(incident_id, f"diagnosis rejected: {exc}")

        self.store.add_diagnosis(incident_id, diagnosis)
        self.store.set_status(incident_id, Status.DIAGNOSED, f"diagnosed: {diagnosis.cause}")

        if diagnosis.recommended is None:
            return self._resolve_or_escalate(incident_id, diagnosis)

        return self._authorise(incident_id, incident.node, diagnosis)

    def _resolve_or_escalate(self, incident_id: str, diagnosis: Diagnosis) -> LoopResult:
        """No action was proposed. That is sometimes the answer, and sometimes
        an admission that a human is needed."""
        benign = diagnosis.cause.startswith("no fault")
        if benign:
            self.store.set_status(
                incident_id,
                Status.RESOLVED,
                "closed without action: no fault found",
                resolution=diagnosis.cause,
            )
            return LoopResult(
                incident_id, Status.RESOLVED, diagnosis=diagnosis, note=diagnosis.cause
            )

        return self._escalate(
            incident_id,
            f"{diagnosis.cause} — no remediation available for this cause, "
            "so it needs a person",
            diagnosis=diagnosis,
        )

    # -- authorisation ------------------------------------------------------

    def _authorise(self, incident_id: str, node: str, diagnosis: Diagnosis) -> LoopResult:
        incident = self.store.get(incident_id, with_timeline=False)
        action = diagnosis.recommended

        ruling = self.policy.rule(
            tier=action.tier,
            action=action.name,
            node=node,
            attempts=incident.attempts,
            active_nodes=len(self.store.nodes_under_remediation() - {node}),
        )

        reason = f"{diagnosis.cause} (confidence {diagnosis.confidence})"

        if ruling.decision is Decision.DENY:
            action_id = self.store.record_action(
                incident_id, node, action, ActionStatus.DENIED, reason, ruling.reason
            )
            return self._escalate(
                incident_id,
                f"remediation refused by policy: {ruling.reason}",
                diagnosis=diagnosis,
                action=action,
                action_id=action_id,
                decision=ruling.decision,
            )

        if ruling.decision is Decision.NEEDS_APPROVAL:
            action_id = self.store.record_action(
                incident_id,
                node,
                action,
                ActionStatus.AWAITING_APPROVAL,
                reason,
                ruling.reason,
                attempt=incident.attempts + 1,
            )
            self.store.set_status(
                incident_id,
                Status.AWAITING_APPROVAL,
                f"awaiting human approval for {action.describe()}",
            )
            return LoopResult(
                incident_id,
                Status.AWAITING_APPROVAL,
                diagnosis=diagnosis,
                action=action,
                action_id=action_id,
                decision=ruling.decision,
                note=ruling.reason,
            )

        action_id = self.store.record_action(
            incident_id,
            node,
            action,
            ActionStatus.APPROVED,
            reason,
            ruling.reason,
            attempt=incident.attempts + 1,
        )
        return self._dispatch(incident_id, node, action, action_id, diagnosis, ruling.decision)

    def _check_approval(self, incident_id: str) -> LoopResult:
        """An incident waiting on a human. Has anyone answered?"""
        pending = [
            a
            for a in self.store.actions(incident_id)
            if a["status"] == ActionStatus.AWAITING_APPROVAL.value
        ]
        if not pending:
            approved = [
                a
                for a in self.store.actions(incident_id)
                if a["status"] == ActionStatus.APPROVED.value
            ]
            if approved:
                record = approved[-1]
                from .actions import build

                return self._dispatch(
                    incident_id,
                    record["node"],
                    build(record["action"], record["arguments"]),
                    record["id"],
                    None,
                    Decision.NEEDS_APPROVAL,
                )
            return self._escalate(incident_id, "approval was rejected")

        record = pending[-1]
        if self.policy.approval_expired(record["created_ts"]):
            self.store.set_action_status(record["id"], ActionStatus.EXPIRED)
            return self._escalate(
                incident_id,
                f"nobody answered the approval request within "
                f"{self.policy.approval_ttl_s:.0f}s; the machine's state has "
                "moved on and the action must be re-proposed rather than run late",
            )

        return LoopResult(
            incident_id,
            Status.AWAITING_APPROVAL,
            action_id=record["id"],
            note="still waiting for a human",
        )

    # -- execution ----------------------------------------------------------

    def _dispatch(
        self,
        incident_id: str,
        node: str,
        action: Action,
        action_id: int,
        diagnosis: Diagnosis | None,
        decision: Decision,
    ) -> LoopResult:
        """Queue the action for the node to collect.

        Nothing is pushed. The node polls, claims, executes from its own
        allowlist, and reports back. The cooldown starts here rather than on
        success, because a failed action still perturbed the machine.
        """
        self.store.bump_attempts(incident_id)
        self.policy.record_execution(node, action.name)
        self.store.set_status(
            incident_id,
            Status.REMEDIATING,
            f"{action.describe()} queued for {node}",
        )
        return LoopResult(
            incident_id,
            Status.REMEDIATING,
            diagnosis=diagnosis,
            action=action,
            action_id=action_id,
            decision=decision,
            note="queued; the node will collect it on its next poll",
        )

    def report_result(
        self, action_id: int, ok: bool, detail: dict[str, Any] | None = None
    ) -> LoopResult:
        """A node has reported back. Move to verification, never to resolved.

        The important line in this method is that ``ok`` does not resolve
        anything. A successful command moves the incident to VERIFYING, where
        the machine gets measured. Only a measurement can close an incident.
        """
        record = self.store.get_action(action_id)
        if record is None:
            raise KeyError(f"no such action: {action_id}")

        incident_id = record["incident_id"]
        self.store.set_action_status(
            action_id,
            ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED,
            result=detail or {},
        )

        if not ok:
            return self._retry_or_escalate(
                incident_id, f"{record['action']} failed on {record['node']}"
            )

        self.store.set_status(
            incident_id,
            Status.VERIFYING,
            f"{record['action']} reported success — verifying the machine actually recovered",
        )
        return LoopResult(
            incident_id,
            Status.VERIFYING,
            action_id=action_id,
            note="command succeeded; recovery not yet established",
        )

    # -- verification -------------------------------------------------------

    def _verify(self, incident_id: str) -> LoopResult:
        incident = self.store.get(incident_id, with_timeline=False)
        evidence = self.store.evidence(incident_id)

        baseline = next(
            (e["result"] for e in evidence if e["tool"] in ("get_node_health", "get_health")),
            {},
        )

        try:
            after = self.tools["get_node_health"].handler(node=incident.node)
        except Exception as exc:  # noqa: BLE001
            return self._retry_or_escalate(
                incident_id, f"could not measure {incident.node} after remediation: {exc}"
            )

        self.store.add_evidence(incident_id, "get_node_health", {"node": incident.node}, after)

        actions = self.store.actions(incident_id)
        last = actions[-1] if actions else None
        expectation = ""
        if last:
            from .actions import ACTIONS

            spec = ACTIONS.get(last["action"])
            expectation = spec.expects if spec else ""

        resource = baseline.get("bottleneck") or "memory"
        result = verify_recovery(
            after, baseline, resource=resource, thresholds=self.t, expectation=expectation
        )
        self.store.add_verification(incident_id, last["id"] if last else None, result)

        if result.recovered:
            self.store.set_status(
                incident_id,
                Status.RESOLVED,
                "verified recovered",
                resolution=result.summary,
            )
            return LoopResult(
                incident_id, Status.RESOLVED, verification=result, note=result.summary
            )

        return self._retry_or_escalate(
            incident_id,
            f"remediation did not restore the machine: {result.summary}",
            verification=result,
        )

    # -- the terminating edges ----------------------------------------------

    def _retry_or_escalate(
        self,
        incident_id: str,
        why: str,
        verification: VerificationResult | None = None,
    ) -> LoopResult:
        """The only path back toward remediation, and it is bounded.

        Every unsuccessful outcome comes through here, and it consults the
        attempt counter that ``_dispatch`` incremented. That is what makes an
        endless retry loop structurally impossible rather than merely unlikely.
        """
        incident = self.store.get(incident_id, with_timeline=False)

        if incident.attempts >= self.policy.max_attempts_per_incident:
            return self._escalate(
                incident_id,
                f"{why}. {incident.attempts} attempt(s) made, limit is "
                f"{self.policy.max_attempts_per_incident}; a human should look at this",
                verification=verification,
            )

        self.store.set_status(
            incident_id,
            Status.OPEN,
            f"{why}. Re-investigating (attempt {incident.attempts + 1} of "
            f"{self.policy.max_attempts_per_incident})",
        )
        return LoopResult(
            incident_id, Status.OPEN, verification=verification, note=f"{why}; will retry"
        )

    def _escalate(
        self,
        incident_id: str,
        why: str,
        diagnosis: Diagnosis | None = None,
        action: Action | None = None,
        action_id: int | None = None,
        decision: Decision | None = None,
        verification: VerificationResult | None = None,
    ) -> LoopResult:
        self.store.set_status(
            incident_id, Status.ESCALATED, f"escalated: {why}", resolution=why
        )
        return LoopResult(
            incident_id,
            Status.ESCALATED,
            diagnosis=diagnosis,
            action=action,
            action_id=action_id,
            decision=decision,
            verification=verification,
            note=why,
        )

    # -- driving many -------------------------------------------------------

    def tick(self) -> list[LoopResult]:
        """Detect, then advance every active incident by one step."""
        self.detect()
        return [self.advance(incident.id) for incident in self.store.active()]

    def run_until_settled(self, max_passes: int = 20) -> list[LoopResult]:
        """Advance repeatedly until nothing is left that can move.

        The bound is a safety net, not the mechanism: the policy guards
        terminate incidents on their own. If this ever hits ``max_passes``
        something is wrong with those guards, and it says so rather than
        spinning.
        """
        results: list[LoopResult] = []
        for _ in range(max_passes):
            active = [
                i
                for i in self.store.active()
                if i.status is not Status.AWAITING_APPROVAL
            ]
            if not active:
                return results
            results.extend(self.advance(i.id) for i in active)
        raise RuntimeError(
            f"incidents still moving after {max_passes} passes — a policy guard "
            "is not terminating. This is a bug, not a busy fleet."
        )

    # -- helpers ------------------------------------------------------------

    def _nodes(self) -> list[str]:
        lister = self.tools.get("list_nodes")
        if lister is None:
            return []
        return [n["node"] for n in lister.handler().get("nodes", [])]


def now_clock() -> str:
    return time.strftime("%H:%M:%S")
