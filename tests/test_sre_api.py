"""Tests for the HTTP surface of the SRE layer.

The API is where a human approves a destructive action and where a node
collects work, so its failure modes matter more than its happy paths. Most of
what is asserted here is what the endpoints *refuse*.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from syshealth.config import Settings
from syshealth.mcp import load_run
from syshealth.sre.actions import build
from syshealth.sre.incidents import ActionStatus, IncidentStore, Severity, Status
from syshealth.store import Store

flask = pytest.importorskip("flask", reason="needs the server extra")

RUNS = Path(__file__).parent / "fixtures" / "runs"


@pytest.fixture
def app(tmp_path: Path):
    from syshealth.server import create_app

    telemetry = Store(tmp_path / "fleet.db")
    now = time.time()
    samples = load_run(RUNS / "thrashing.jsonl")
    for index, sample in enumerate(samples):
        telemetry.record(
            "web-01", sample.to_dict(), "t3.micro", now=now - (len(samples) - index) * 2.0
        )

    incidents = IncidentStore(tmp_path / "incidents.db")
    settings = Settings(db_path=str(tmp_path / "fleet.db"))
    application = create_app(settings, store=telemetry, incidents=incidents)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def pending(app):
    """An incident with one action waiting on a human."""
    record = app.config["INCIDENTS"]
    incident = record.open_incident(
        node="web-01", severity=Severity.CRITICAL, title="memory saturated", mode="ASSIST"
    )
    action_id = record.record_action(
        incident.id,
        "web-01",
        build("restart_service", {"service": "app"}),
        ActionStatus.AWAITING_APPROVAL,
        reason="memory exhaustion (confidence HIGH)",
        ruling="mode is ASSIST: proposed for human approval",
    )
    record.set_status(incident.id, Status.AWAITING_APPROVAL)
    return incident.id, action_id


# ------------------------------------------------------------- incidents ---


def test_incidents_are_listed(client, pending):
    body = client.get("/incidents").get_json()
    assert body["count"] == 1
    assert body["active"] == 1
    assert body["incidents"][0]["node"] == "web-01"


def test_an_incident_report_contains_the_whole_story(client, pending):
    incident_id, _ = pending
    body = client.get(f"/incidents/{incident_id}").get_json()

    assert set(body) == {"incident", "evidence", "diagnoses", "actions", "verifications"}
    assert body["actions"][0]["reason"].startswith("memory exhaustion")
    assert body["incident"]["timeline"]


def test_an_unknown_incident_is_a_404(client):
    assert client.get("/incidents/INC-9999").status_code == 404


def test_an_unknown_status_filter_is_refused(client):
    assert client.get("/incidents?status=BANANA").status_code == 400


# ------------------------------------------------------------- approvals ---


def test_a_pending_action_is_listed_with_everything_needed_to_decide(client, pending):
    body = client.get("/approvals").get_json()

    assert body["count"] == 1
    action = body["actions"][0]
    assert action["action"] == "restart_service"
    assert action["reason"], "a human must be told why"
    assert action["ruling"], "and under what policy"
    assert action["tier"] == "LOW_RISK"


def test_approving_an_action_makes_it_collectable(client, app, pending):
    _, action_id = pending

    response = client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "alok"})

    assert response.status_code == 200
    assert app.config["INCIDENTS"].get_action(action_id)["decided_by"] == "alok"
    assert client.get("/nodes/web-01/actions/next").get_json()["id"] == action_id


def test_rejecting_an_action_leaves_nothing_to_collect(client, pending):
    _, action_id = pending

    client.post(f"/actions/{action_id}/decision", json={"approve": False, "by": "alok"})

    assert client.get("/nodes/web-01/actions/next").get_json() == {}


def test_an_action_cannot_be_decided_twice(client, pending):
    """A second approval must not resurrect a rejected action."""
    _, action_id = pending
    client.post(f"/actions/{action_id}/decision", json={"approve": False, "by": "alok"})

    second = client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "mallory"})

    assert second.status_code == 409
    assert "not awaiting approval" in second.get_json()["error"]


def test_a_decision_must_actually_be_a_decision(client, pending):
    _, action_id = pending
    for body in ({}, {"approve": "yes"}, {"by": "alok"}, {"approve": None}):
        assert client.post(f"/actions/{action_id}/decision", json=body).status_code == 400


def test_deciding_an_unknown_action_is_a_404(client):
    assert client.post("/actions/424242/decision", json={"approve": True}).status_code == 404


# ---------------------------------------------------------- action queue ---


def test_a_node_gets_nothing_when_nothing_is_approved(client, pending):
    assert client.get("/nodes/web-01/actions/next").get_json() == {}


def test_an_action_is_handed_out_exactly_once(client, pending):
    _, action_id = pending
    client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "alok"})

    assert client.get("/nodes/web-01/actions/next").get_json()["id"] == action_id
    assert client.get("/nodes/web-01/actions/next").get_json() == {}


def test_a_node_only_sees_its_own_work(client, pending):
    _, action_id = pending
    client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "alok"})

    assert client.get("/nodes/other-01/actions/next").get_json() == {}


def test_a_successful_command_moves_to_verifying_not_resolved(client, app, pending):
    """The property the whole verification design exists to protect."""
    incident_id, action_id = pending
    client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "alok"})
    client.get("/nodes/web-01/actions/next")

    response = client.post(
        f"/actions/{action_id}/result", json={"ok": True, "detail": {"returncode": 0}}
    )

    assert response.get_json()["next"] == "VERIFYING"
    incident = app.config["INCIDENTS"].get(incident_id, with_timeline=False)
    assert incident.status is Status.VERIFYING
    assert incident.status is not Status.RESOLVED
    assert not incident.resolution


def test_a_failed_command_reopens_the_incident(client, app, pending):
    incident_id, action_id = pending
    client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "alok"})
    client.get("/nodes/web-01/actions/next")

    client.post(f"/actions/{action_id}/result", json={"ok": False, "detail": {"error": "no unit"}})

    assert app.config["INCIDENTS"].get(incident_id, with_timeline=False).status is Status.OPEN


def test_a_result_cannot_be_reported_twice(client, pending):
    _, action_id = pending
    client.post(f"/actions/{action_id}/decision", json={"approve": True, "by": "alok"})
    client.get("/nodes/web-01/actions/next")
    client.post(f"/actions/{action_id}/result", json={"ok": True})

    replayed = client.post(f"/actions/{action_id}/result", json={"ok": True})

    assert replayed.status_code == 409


def test_a_result_cannot_be_reported_for_an_undispatched_action(client, pending):
    """Otherwise anything that can reach the API can mark work done."""
    _, action_id = pending
    assert client.post(f"/actions/{action_id}/result", json={"ok": True}).status_code == 409


# --------------------------------------------------------------- surface ---


def test_the_catalogue_is_published(client):
    body = client.get("/actions/catalogue").get_json()
    names = {a["name"] for a in body["actions"]}

    assert "restart_service" in names
    assert not ({"run_command", "exec", "shell"} & names)


def test_the_dashboard_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"SysHealth" in response.data


def test_the_telemetry_endpoints_still_work(client):
    """The SRE layer must not have broken the fleet API underneath it."""
    assert client.get("/healthz").get_json()["ok"] is True
    assert client.get("/nodes").get_json()[0]["node"] == "web-01"
    assert client.get("/nodes/web-01/verdict").get_json()["sizing"] == "UNDERSIZED"
    assert client.get("/fleet").get_json()["undersized"] == 1
