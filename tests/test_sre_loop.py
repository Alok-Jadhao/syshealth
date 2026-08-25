"""End-to-end tests for the incident loop.

Built on a scripted fake fleet rather than the real tools, so a test can say
"this node is thrashing, then recovers after a restart" and check that the
loop reaches the right conclusion for the right reason. The real tools are
exercised in the integration test at the bottom.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from syshealth.mcp.tools import Tier, Tool
from syshealth.sre.detect import Detector
from syshealth.sre.incidents import ActionStatus, IncidentStore, Status
from syshealth.sre.loop import IncidentLoop
from syshealth.sre.policy import Decision, Mode, Policy
from syshealth.sre.reason import RuleReasoner

RUNS = Path(__file__).parent / "fixtures" / "runs"


# --------------------------------------------------------------- fake data --


def health(
    node: str = "web-01",
    state: str = "SATURATED",
    memory_p95: float = 16.9,
    oom: int = 0,
    swap_in: int = 0,
    direct: float = 900.0,
    naive: float = 99.0,
    psi: bool = True,
    online: bool = True,
    bottleneck: str | None = "memory",
) -> dict[str, Any]:
    return {
        "node": node,
        "instance_type": "t3.micro",
        "online": online,
        "seconds_since_last_sample": 2.0,
        "window": {"samples": 90, "duration_s": 180.0},
        "psi_available": psi,
        "state": state,
        "bottleneck": bottleneck,
        "resources": {
            "memory": {
                "state": state,
                "some_p50_pct": memory_p95 * 0.9,
                "some_p95_pct": memory_p95,
                "some_max_pct": memory_p95 * 1.01,
                "full_max_pct": 5.9 if memory_p95 > 10 else 0.0,
                "stalled_seconds": 25.1,
            },
            "io": {
                "state": "HEALTHY",
                "some_p50_pct": 0.1,
                "some_p95_pct": 0.4,
                "some_max_pct": 0.5,
                "full_max_pct": 0.0,
                "stalled_seconds": 0.7,
            },
            "cpu": {
                "state": "HEALTHY",
                "some_p50_pct": 0.2,
                "some_p95_pct": 0.5,
                "some_max_pct": 0.6,
                "full_max_pct": 0.0,
                "stalled_seconds": 0.9,
            },
        },
        "utilisation": {
            "working_set_pct_mean": 90.0,
            "working_set_pct_max": 95.0,
            "naive_used_pct_max": naive,
            "swap_used_pct_max": 10.0,
            "cpu_busy_pct_mean": 61.0,
            "cpu_busy_pct_max": 74.6,
            "total_gb": 0.95,
        },
        "reclaim": {
            "direct_reclaim_total": int(direct * 180),
            "direct_reclaim_per_s": direct,
            "major_faults_total": 20836,
            "swap_in_total": swap_in,
            "oom_kills": oom,
        },
        "divergence_pct_points": naive - memory_p95,
        "note": "State comes from the p95 of 90 samples covering 180.0s",
    }


HEALTHY = health(state="HEALTHY", memory_p95=0.02, oom=0, swap_in=0, direct=0.0, naive=40.0, bottleneck=None)
SICK = health(oom=3, swap_in=9431)


class FakeFleet:
    """A scripted fleet. Each node yields readings in order, repeating the last."""

    def __init__(self, script: dict[str, list[dict]]) -> None:
        self.script = script
        self.calls: dict[str, int] = dict.fromkeys(script, 0)

    def tools(self) -> dict[str, Tool]:
        def list_nodes() -> dict:
            return {"count": len(self.script), "nodes": [{"node": n} for n in self.script]}

        def get_node_health(node: str, max_samples: int = 600) -> dict:
            if node not in self.script:
                raise KeyError(node)
            index = min(self.calls[node], len(self.script[node]) - 1)
            self.calls[node] += 1
            return self.script[node][index]

        def get_node_verdict(node: str, max_samples: int = 2000) -> dict:
            return {
                "node": node,
                "sizing": "UNDERSIZED",
                "confidence": "HIGH",
                "headline": "Undersized on memory.",
                "reasons": ["the working set does not fit"],
                "evidence": ["Memory stall: p95 16.90%"],
                "caveats": [],
            }

        return {
            "list_nodes": Tool("list_nodes", Tier.READ_ONLY, "nodes", list_nodes),
            "get_node_health": Tool("get_node_health", Tier.READ_ONLY, "health", get_node_health),
            "get_node_verdict": Tool(
                "get_node_verdict", Tier.READ_ONLY, "verdict", get_node_verdict
            ),
        }


def make_loop(
    script: dict[str, list[dict]],
    mode: Mode = Mode.AUTONOMOUS,
    tmp_path: Path | None = None,
    managed: dict | None = None,
    **policy_kwargs,
) -> tuple[IncidentLoop, IncidentStore, FakeFleet]:
    fleet = FakeFleet(script)
    store = IncidentStore(tmp_path / "incidents.db" if tmp_path else ":memory:")
    policy = Policy(
        mode=mode,
        autonomous_actions=frozenset({"restart_service"}),
        **policy_kwargs,
    )
    loop = IncidentLoop(
        tools=fleet.tools(),
        store=store,
        policy=policy,
        reasoner=RuleReasoner(),
        detector=Detector(consecutive=1),
        managed=managed if managed is not None else {"web-01": {"service": "app"}},
    )
    return loop, store, fleet


# ------------------------------------------------------------- detection ---


def test_detection_opens_an_incident_for_a_saturated_node(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK]}, tmp_path=tmp_path)

    opened = loop.detect()

    assert len(opened) == 1
    incident = store.get(opened[0])
    assert incident.node == "web-01"
    assert incident.severity.value == "CRITICAL"
    assert "OOM" in incident.title


def test_detection_does_not_open_anything_for_a_healthy_node(tmp_path: Path):
    loop, _, _ = make_loop({"web-01": [HEALTHY]}, tmp_path=tmp_path)
    assert loop.detect() == []


def test_the_same_symptom_does_not_open_a_second_incident(tmp_path: Path):
    """A detector running every few seconds must not open hundreds of
    incidents for one ongoing problem."""
    loop, store, _ = make_loop({"web-01": [SICK, SICK, SICK, SICK]}, tmp_path=tmp_path)

    first = loop.detect()
    second = loop.detect()

    assert len(first) == 1
    assert second == []
    assert len(store.list_incidents()) == 1


def test_hysteresis_requires_the_symptom_to_persist():
    """One bad reading between two good ones must not open an incident."""
    detector = Detector(consecutive=2)

    assert detector.examine(SICK) is None, "first sighting should not fire"
    assert detector.examine(SICK) is not None, "second consecutive sighting fires"


def test_a_recovery_between_sightings_resets_the_streak():
    detector = Detector(consecutive=2)

    detector.examine(SICK)
    detector.examine(HEALTHY)

    assert detector.examine(SICK) is None, "the streak must have been cleared"


def test_an_unmeasurable_node_is_a_warning_not_a_saturation_incident():
    detector = Detector(consecutive=1)
    finding = detector.examine(health(psi=False))

    assert finding is not None
    assert finding.symptom == "no-psi"
    assert finding.severity.value == "WARNING"


# ------------------------------------------------------------- diagnosis ---


def test_diagnosis_cites_evidence_that_was_actually_collected(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, mode=Mode.OBSERVE, tmp_path=tmp_path)
    incident_id = loop.detect()[0]

    loop.advance(incident_id)

    diagnoses = store.diagnoses(incident_id)
    evidence_ids = {e["id"] for e in store.evidence(incident_id)}

    assert diagnoses
    assert diagnoses[0]["cites"]
    assert set(diagnoses[0]["cites"]) <= evidence_ids


def test_a_diagnosis_citing_evidence_that_does_not_exist_is_rejected(tmp_path: Path):
    """The mechanism behind 'the AI must not invent a root cause'.

    A reasoner that fabricates a citation must not have its conclusion
    recorded, and the incident must go to a human.
    """
    from syshealth.sre.reason import Diagnosis

    class Fabricator:
        name = "fabricator"

        def investigate(self, context):
            context.call("get_node_health", node=context.node)
            return Diagnosis(
                cause="definitely a database connection leak",
                confidence="HIGH",
                reasoner=self.name,
                observations=["postgres had 96/100 connections"],
                cites=[9999],
            )

    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, tmp_path=tmp_path)
    loop.reasoner = Fabricator()
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.status is Status.ESCALATED
    assert "never collected" in result.note
    assert store.diagnoses(incident_id) == [], "a rejected diagnosis must not be recorded"


def test_a_diagnosis_with_no_citations_at_all_is_rejected(tmp_path: Path):
    from syshealth.sre.reason import Diagnosis, DiagnosisError

    diagnosis = Diagnosis(
        cause="something", confidence="HIGH", reasoner="x", observations=["a"], cites=[]
    )
    with pytest.raises(DiagnosisError, match="must cite the evidence"):
        diagnosis.validate(available={1, 2})


def test_high_utilisation_with_no_stalling_is_diagnosed_as_no_fault(tmp_path: Path):
    """The false alarm must not become an incident that restarts anything."""
    calm = health(
        state="HEALTHY", memory_p95=0.03, direct=0.0, naive=94.0, bottleneck=None
    )
    loop, store, _ = make_loop({"web-01": [calm]}, tmp_path=tmp_path)

    from syshealth.sre.detect import Finding
    from syshealth.sre.incidents import Severity

    incident_id = loop.open(
        Finding("web-01", Severity.WARNING, "manual", "web-01:manual", "manual", {})
    )
    result = loop.advance(incident_id)

    assert result.status is Status.RESOLVED
    assert "no fault" in result.diagnosis.cause
    assert result.diagnosis.recommended is None
    assert store.actions(incident_id) == [], "nothing should have been proposed"


def test_an_unmeasurable_node_yields_a_low_confidence_diagnosis_and_no_action(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [health(psi=False)]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.diagnosis.confidence == "LOW"
    assert result.diagnosis.recommended is None
    assert result.status is Status.ESCALATED


def test_no_declared_target_means_no_restart_is_proposed(tmp_path: Path):
    """A restart aimed at a guessed name is worse than proposing nothing."""
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, tmp_path=tmp_path, managed={})
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.status is Status.ESCALATED
    assert result.diagnosis.recommended is None
    assert "needs a person" in result.note


# ------------------------------------------------------------ the modes ----


def test_observe_mode_investigates_and_then_refuses_to_act(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, mode=Mode.OBSERVE, tmp_path=tmp_path)
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.status is Status.ESCALATED
    assert result.decision is Decision.DENY
    assert result.diagnosis is not None, "it must still have investigated"

    actions = store.actions(incident_id)
    assert [a["status"] for a in actions] == [ActionStatus.DENIED.value]


def test_assist_mode_proposes_and_waits(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, mode=Mode.ASSIST, tmp_path=tmp_path)
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.status is Status.AWAITING_APPROVAL
    assert result.decision is Decision.NEEDS_APPROVAL
    assert result.action.name == "restart_service"

    pending = store.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["reason"].startswith("memory exhaustion")


def test_assist_mode_does_not_dispatch_while_waiting(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK] * 5}, mode=Mode.ASSIST, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    loop.advance(incident_id)

    for _ in range(3):
        result = loop.advance(incident_id)
        assert result.status is Status.AWAITING_APPROVAL

    assert store.claim_next_action("web-01") is None, "nothing may be collectable"


def test_autonomous_mode_dispatches_a_permitted_action(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.status is Status.REMEDIATING
    assert result.decision is Decision.ALLOW

    claimed = store.claim_next_action("web-01")
    assert claimed["action"] == "restart_service"
    assert claimed["arguments"] == {"service": "app"}


# ------------------------------------------------------------- approval ----


def test_an_approved_action_is_dispatched_on_the_next_pass(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK] * 5}, mode=Mode.ASSIST, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    proposed = loop.advance(incident_id)

    store.set_action_status(proposed.action_id, ActionStatus.APPROVED, decided_by="alok")
    result = loop.advance(incident_id)

    assert result.status is Status.REMEDIATING
    assert store.claim_next_action("web-01")["id"] == proposed.action_id


def test_a_rejected_action_escalates(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK] * 5}, mode=Mode.ASSIST, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    proposed = loop.advance(incident_id)

    store.set_action_status(proposed.action_id, ActionStatus.REJECTED, decided_by="alok")
    result = loop.advance(incident_id)

    assert result.status is Status.ESCALATED
    assert store.claim_next_action("web-01") is None


def test_an_unanswered_approval_expires_rather_than_running_late(tmp_path: Path):
    """The machine's state has moved on; a stale yes must not execute."""
    loop, store, _ = make_loop(
        {"web-01": [SICK] * 5}, mode=Mode.ASSIST, tmp_path=tmp_path
    )
    loop.policy.approval_ttl_s = 0.0
    incident_id = loop.detect()[0]
    loop.advance(incident_id)

    result = loop.advance(incident_id)

    assert result.status is Status.ESCALATED
    assert "nobody answered" in result.note


# ---------------------------------------------------------- verification ---


def test_a_successful_command_does_not_resolve_the_incident(tmp_path: Path):
    """The rule the whole verification design exists for."""
    loop, store, _ = make_loop({"web-01": [SICK, SICK, SICK]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    dispatched = loop.advance(incident_id)

    result = loop.report_result(dispatched.action_id, ok=True)

    assert result.status is Status.VERIFYING
    assert result.status is not Status.RESOLVED
    assert store.get(incident_id).status is Status.VERIFYING


def test_recovery_is_established_by_measurement(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK, SICK, HEALTHY]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    dispatched = loop.advance(incident_id)
    loop.report_result(dispatched.action_id, ok=True)

    result = loop.advance(incident_id)

    assert result.status is Status.RESOLVED
    assert result.verification.recovered
    assert "recovered" in result.verification.summary


def test_a_command_that_worked_but_did_not_help_is_not_a_resolution(tmp_path: Path):
    """restart_container -> success, and the machine is still thrashing.

    This is the case the brief calls out specifically, and the loop must
    retry or escalate rather than close the incident.
    """
    loop, store, _ = make_loop({"web-01": [SICK] * 6}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    dispatched = loop.advance(incident_id)
    loop.report_result(dispatched.action_id, ok=True)

    result = loop.advance(incident_id)

    assert result.status is not Status.RESOLVED
    assert not result.verification.recovered
    assert store.get(incident_id).status in (Status.OPEN, Status.ESCALATED)


def test_a_node_that_went_silent_after_a_restart_is_not_recovered(tmp_path: Path):
    """A machine that stopped answering is a worse outcome, not a fixed one."""
    silent = health(state="HEALTHY", memory_p95=0.0, direct=0.0, online=False, bottleneck=None)
    loop, store, _ = make_loop({"web-01": [SICK, SICK, silent]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    dispatched = loop.advance(incident_id)
    loop.report_result(dispatched.action_id, ok=True)

    result = loop.advance(incident_id)

    assert result.status is not Status.RESOLVED
    failed = [c.name for c in result.verification.failed]
    assert "node is still reporting" in failed


def test_a_failed_command_does_not_go_to_verification(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK] * 6}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    dispatched = loop.advance(incident_id)

    result = loop.report_result(dispatched.action_id, ok=False, detail={"error": "no such unit"})

    assert result.status in (Status.OPEN, Status.ESCALATED)
    assert store.get_action(dispatched.action_id)["status"] == ActionStatus.FAILED.value


# ------------------------------------------------------- loop termination --


def test_remediation_stops_after_the_attempt_limit(tmp_path: Path):
    """The guarantee against an infinite autonomous remediation loop."""
    loop, store, _ = make_loop(
        {"web-01": [SICK] * 40}, tmp_path=tmp_path, max_attempts_per_incident=2, cooldown_s=0.0
    )
    incident_id = loop.detect()[0]

    for _ in range(10):
        incident = store.get(incident_id, with_timeline=False)
        if incident.status.terminal:
            break
        result = loop.advance(incident_id)
        if result.status is Status.REMEDIATING:
            loop.report_result(result.action_id, ok=True)

    incident = store.get(incident_id, with_timeline=False)
    assert incident.status is Status.ESCALATED
    assert incident.attempts <= 2

    dispatched = [
        a for a in store.actions(incident_id) if a["status"] != ActionStatus.DENIED.value
    ]
    assert len(dispatched) <= 2, "must not have queued more actions than the limit"


def test_the_loop_settles_rather_than_spinning(tmp_path: Path):
    """run_until_settled raises if a guard fails to terminate. It must not."""
    loop, store, _ = make_loop({"web-01": [SICK] * 60}, tmp_path=tmp_path, cooldown_s=0.0)
    loop.detect()

    results = loop.run_until_settled(max_passes=20)

    assert results
    assert all(i.status.terminal for i in store.list_incidents())


def test_an_incident_open_too_long_is_escalated(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK] * 5}, mode=Mode.ASSIST, tmp_path=tmp_path)
    loop.policy.incident_timeout_s = 0.0
    incident_id = loop.detect()[0]

    result = loop.advance(incident_id)

    assert result.status is Status.ESCALATED
    assert "without resolving" in result.note


def test_the_cooldown_blocks_a_second_incident_from_acting(tmp_path: Path):
    """Two incidents on correlated symptoms must not become a restart loop."""
    loop, store, _ = make_loop({"web-01": [SICK] * 10}, tmp_path=tmp_path)
    first = loop.detect()[0]
    loop.advance(first)

    from syshealth.sre.detect import Finding
    from syshealth.sre.incidents import Severity

    second = loop.open(
        Finding("web-01", Severity.CRITICAL, "another", "web-01:other", "other", {})
    )
    result = loop.advance(second)

    assert result.status is Status.ESCALATED
    assert "cooldown" in result.note


# -------------------------------------------------------------- the audit --


def test_the_audit_trail_answers_the_three_questions(tmp_path: Path):
    """'Why did it restart this?', 'on what evidence?', 'what happened next?'"""
    loop, store, _ = make_loop({"web-01": [SICK, SICK, HEALTHY]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    dispatched = loop.advance(incident_id)
    loop.report_result(dispatched.action_id, ok=True)
    loop.advance(incident_id)

    report = store.report(incident_id)

    # why
    action = report["actions"][-1]
    assert action["action"] == "restart_service"
    assert action["reason"].startswith("memory exhaustion")
    assert "AUTONOMOUS" in action["ruling"]

    # on what evidence
    assert report["evidence"]
    cited = set(report["diagnoses"][0]["cites"])
    assert cited <= {e["id"] for e in report["evidence"]}
    assert all("result" in e for e in report["evidence"])

    # what happened next
    assert report["verifications"][-1]["recovered"] is True

    # and the whole thing is ordered and readable
    kinds = [e["kind"] for e in report["incident"]["timeline"]]
    assert kinds[0] == "detected"
    assert "diagnosis" in kinds and "action" in kinds and "verification" in kinds
    timestamps = [e["ts"] for e in report["incident"]["timeline"]]
    assert timestamps == sorted(timestamps)


def test_every_action_is_recorded_before_it_can_be_collected(tmp_path: Path):
    """There must be no window in which something ran unrecorded."""
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    result = loop.advance(incident_id)

    record = store.get_action(result.action_id)
    assert record is not None
    assert record["created_ts"] <= (record["dispatched_ts"] or float("inf"))
    assert record["reason"] and record["ruling"]


def test_a_denied_action_is_still_written_down(tmp_path: Path):
    """What the system wanted to do and was not allowed to is part of the record."""
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, mode=Mode.OBSERVE, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    loop.advance(incident_id)

    actions = store.actions(incident_id)
    assert len(actions) == 1
    assert actions[0]["status"] == ActionStatus.DENIED.value
    assert "OBSERVE" in actions[0]["ruling"]


def test_an_action_is_handed_out_exactly_once(tmp_path: Path):
    loop, store, _ = make_loop({"web-01": [SICK, SICK]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    loop.advance(incident_id)

    assert store.claim_next_action("web-01") is not None
    assert store.claim_next_action("web-01") is None, "a second poller must get nothing"


# ------------------------------------------------------------ integration --


def test_the_loop_runs_against_the_real_tools(tmp_path: Path):
    """No fakes: the real fleet tools over a store built from recorded runs."""
    import time as time_module

    from syshealth.mcp import build_fleet_tools, load_run
    from syshealth.store import Store

    telemetry = Store(tmp_path / "fleet.db")
    now = time_module.time()
    samples = load_run(RUNS / "thrashing.jsonl")
    for index, sample in enumerate(samples):
        telemetry.record(
            "web-01",
            sample.to_dict(),
            "t3.micro",
            now=now - (len(samples) - index) * 2.0,
        )

    store = IncidentStore(tmp_path / "incidents.db")
    loop = IncidentLoop(
        tools=build_fleet_tools(telemetry),
        store=store,
        policy=Policy(mode=Mode.ASSIST),
        reasoner=RuleReasoner(),
        detector=Detector(consecutive=1),
        managed={"web-01": {"service": "app"}},
    )

    opened = loop.detect()
    assert len(opened) == 1

    result = loop.advance(opened[0])

    assert result.status is Status.AWAITING_APPROVAL
    assert result.diagnosis.cause.startswith("memory exhaustion")
    assert result.diagnosis.confidence == "HIGH"
    assert result.action.describe() == "restart_service(service='app')"

    report = store.report(opened[0])
    assert len(report["evidence"]) >= 2, "health and verdict should both be cited"
    assert set(report["diagnoses"][0]["cites"]) <= {e["id"] for e in report["evidence"]}


# ------------------------------- regressions found by the chaos environment --


def test_the_incident_is_titled_by_the_worst_resource_not_the_first():
    """Found by chaos: a container stalling 2.9% on memory and 52% on io was
    reported as a memory incident, and the investigation then contradicted the
    incident it was opened for."""
    mixed = health(state="SATURATED", memory_p95=2.9, bottleneck="io")
    mixed["resources"]["memory"]["full_max_pct"] = 3.0
    mixed["resources"]["io"].update(some_p95_pct=52.1, some_max_pct=56.6, full_max_pct=0.0)

    finding = Detector(consecutive=1).examine(mixed)

    assert finding is not None
    assert finding.symptom == "io-saturated"
    assert "io saturated" in finding.title
    assert "memory" in finding.detail["also_saturated"]


def test_memory_saturation_via_full_stall_is_diagnosed_not_missed(tmp_path: Path):
    """Found by chaos: a container whose full-stall reached 3% — windows in
    which nothing ran — fell through the reasoner's `some`-only test."""
    stalled = health(state="SATURATED", memory_p95=2.9, oom=0, swap_in=0, direct=114214.0)
    stalled["resources"]["memory"]["full_max_pct"] = 3.0
    stalled["resources"]["io"].update(some_p95_pct=0.4, full_max_pct=0.0)
    stalled["bottleneck"] = "memory"

    loop, store, _ = make_loop({"web-01": [stalled, stalled]}, tmp_path=tmp_path)
    incident_id = loop.detect()[0]
    result = loop.advance(incident_id)

    assert "memory saturation" in result.diagnosis.cause
    assert result.diagnosis.confidence == "MEDIUM"
    assert any("full-stall" in h for h in result.diagnosis.hypotheses)


def test_the_two_saturation_tests_agree():
    """The reasoner and the classifier must not drift apart on what SATURATED
    means, or an incident can be opened that the reasoner then denies."""
    from syshealth.analysis import Thresholds

    t = Thresholds()
    reasoner = RuleReasoner(t)

    for some, full in ((0.0, 0.0), (11.0, 0.0), (0.0, 2.5), (2.9, 3.0), (9.9, 1.9)):
        expected = some >= t.some_saturated or full >= t.full_saturated
        assert reasoner._memory_saturated(some, full) is expected, (some, full)
