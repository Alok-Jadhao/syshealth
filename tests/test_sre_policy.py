"""Tests for the policy engine and the action catalogue.

These are the tests that matter most in the package. Everything else decides
what to suggest; this decides what may happen to a real machine.
"""

from __future__ import annotations

import pytest

from syshealth.mcp.tools import Tier, ToolInputError
from syshealth.sre.actions import ACTIONS, build, catalogue
from syshealth.sre.policy import Decision, Mode, Policy


@pytest.fixture
def assist() -> Policy:
    return Policy(mode=Mode.ASSIST)


@pytest.fixture
def autonomous() -> Policy:
    return Policy(mode=Mode.AUTONOMOUS, autonomous_actions=frozenset({"restart_service"}))


# ------------------------------------------------------------------ modes --


@pytest.mark.parametrize("mode", list(Mode))
def test_read_only_is_permitted_in_every_mode(mode: Mode):
    ruling = Policy(mode=mode).rule(Tier.READ_ONLY, "get_health", "web-01")
    assert ruling.decision is Decision.ALLOW


@pytest.mark.parametrize("tier", [Tier.LOW_RISK, Tier.HIGH_RISK])
def test_observe_mode_permits_no_change_at_all(tier: Tier):
    ruling = Policy(mode=Mode.OBSERVE).rule(tier, "restart_service", "web-01")
    assert ruling.decision is Decision.DENY
    assert "OBSERVE" in ruling.reason


def test_assist_mode_asks_before_any_change(assist: Policy):
    ruling = assist.rule(Tier.LOW_RISK, "restart_service", "web-01")
    assert ruling.decision is Decision.NEEDS_APPROVAL


def test_autonomous_mode_runs_a_listed_low_risk_action(autonomous: Policy):
    ruling = autonomous.rule(Tier.LOW_RISK, "restart_service", "web-01")
    assert ruling.decision is Decision.ALLOW


def test_autonomous_mode_still_asks_about_an_unlisted_action(autonomous: Policy):
    """Turning autonomy on and choosing what it may do are two decisions.

    An unlisted action degrades to ASSIST rather than being refused, so
    tightening the list cannot silently break incident response.
    """
    ruling = autonomous.rule(Tier.LOW_RISK, "clear_temp_files", "web-01")
    assert ruling.decision is Decision.NEEDS_APPROVAL
    assert "not in the autonomous action list" in ruling.reason


def test_high_risk_always_needs_a_human_even_when_autonomous(autonomous: Policy):
    """The property that must never regress."""
    permissive = Policy(
        mode=Mode.AUTONOMOUS,
        autonomous_actions=frozenset({"terminate_instance", "restart_service"}),
    )
    ruling = permissive.rule(Tier.HIGH_RISK, "terminate_instance", "web-01")

    assert ruling.decision is Decision.NEEDS_APPROVAL
    assert "HIGH_RISK" in ruling.reason


def test_confidence_cannot_widen_permissions():
    """There is no argument for confidence in the signature, by design.

    A reasoner cannot talk its way into a permission, because the policy
    engine is never told what the reasoner believes.
    """
    import inspect

    parameters = set(inspect.signature(Policy.rule).parameters)
    assert "confidence" not in parameters
    assert parameters == {
        "self",
        "tier",
        "action",
        "node",
        "attempts",
        "active_nodes",
        "now",
    }


# ----------------------------------------------------------------- guards --


def test_an_incident_stops_after_its_attempt_limit(autonomous: Policy):
    ruling = autonomous.rule(Tier.LOW_RISK, "restart_service", "web-01", attempts=2)

    assert ruling.decision is Decision.DENY
    assert "escalate" in ruling.reason


def test_the_same_action_cannot_repeat_inside_the_cooldown(autonomous: Policy):
    autonomous.record_execution("web-01", "restart_service", now=1000.0)

    blocked = autonomous.rule(Tier.LOW_RISK, "restart_service", "web-01", now=1060.0)
    assert blocked.decision is Decision.DENY
    assert "cooldown" in blocked.reason

    later = autonomous.rule(Tier.LOW_RISK, "restart_service", "web-01", now=1400.0)
    assert later.decision is Decision.ALLOW


def test_the_cooldown_is_per_node_not_global(autonomous: Policy):
    autonomous.record_execution("web-01", "restart_service", now=1000.0)
    other = autonomous.rule(Tier.LOW_RISK, "restart_service", "web-02", now=1010.0)
    assert other.decision is Decision.ALLOW


def test_a_fleet_wide_symptom_cannot_become_a_fleet_wide_action(autonomous: Policy):
    """A bad deploy degrades every node at once. The blast radius guard is
    what stops the response from taking the whole fleet down with it."""
    ruling = autonomous.rule(Tier.LOW_RISK, "restart_service", "web-02", active_nodes=1)

    assert ruling.decision is Decision.DENY
    assert "fleet-wide" in ruling.reason


def test_guards_are_checked_before_the_approval_path():
    """A human must not be asked to approve the fourth restart in five
    minutes; the guard should have refused it before they saw it."""
    policy = Policy(mode=Mode.ASSIST)
    ruling = policy.rule(Tier.HIGH_RISK, "terminate_instance", "web-01", attempts=99)

    assert ruling.decision is Decision.DENY


def test_an_unanswered_approval_expires(assist: Policy):
    assert not assist.approval_expired(1000.0, now=1100.0)
    assert assist.approval_expired(1000.0, now=1000.0 + assist.approval_ttl_s + 1)


def test_an_incident_open_too_long_expires(assist: Policy):
    assert assist.incident_expired(0.0, now=assist.incident_timeout_s + 1)


# --------------------------------------------------------------- defaults --


def test_the_default_policy_can_do_nothing():
    """A Policy built with no arguments must be inert. If a deployment forgets
    to configure this, the failure should be that nothing happens."""
    policy = Policy()

    assert policy.mode is Mode.OBSERVE
    assert policy.autonomous_actions == frozenset()
    assert policy.rule(Tier.LOW_RISK, "restart_service", "web-01").decision is Decision.DENY


def test_an_unrecognised_mode_is_refused_loudly():
    from syshealth.config import Settings

    with pytest.raises(ValueError, match="mode must be one of"):
        Policy.from_settings(Settings(mode="YOLO"))


def test_settings_build_a_policy():
    from syshealth.config import Settings

    policy = Policy.from_settings(
        Settings(mode="autonomous", autonomous_actions="restart_service, restart_container")
    )

    assert policy.mode is Mode.AUTONOMOUS
    assert policy.autonomous_actions == {"restart_service", "restart_container"}


# -------------------------------------------------------------- catalogue --


def test_there_is_no_arbitrary_execution_action():
    """The most important assertion in the suite.

    If a shell action is ever added, this fails and someone has to argue for
    it in review rather than merge it quietly.
    """
    forbidden = {"run_command", "exec", "shell", "sh", "bash", "eval", "run", "system"}
    assert not (forbidden & set(ACTIONS))

    for spec in ACTIONS.values():
        for arg in spec.args:
            assert arg.name not in {"command", "cmd", "argv", "script", "shell"}, (
                f"{spec.name} takes a free-form command argument"
            )


def test_an_unknown_action_cannot_be_built():
    with pytest.raises(ToolInputError, match="unknown action"):
        build("definitely_not_real", {})


@pytest.mark.parametrize(
    "hostile",
    [
        "app; rm -rf /",
        "app && curl evil.example",
        "../../etc/passwd",
        "$(whoami)",
        "`id`",
        "app\nrm -rf /",
        "a" * 200,
        "",
    ],
)
def test_hostile_names_are_refused(hostile: str):
    with pytest.raises(ToolInputError):
        build("restart_service", {"service": hostile})


def test_unknown_arguments_are_refused_not_ignored():
    """Silently dropping an unexpected argument is how a caller ends up
    believing it constrained something it did not."""
    with pytest.raises(ToolInputError, match="unexpected argument"):
        build("restart_service", {"service": "app", "force": True})


def test_a_missing_required_argument_is_refused():
    with pytest.raises(ToolInputError, match="required"):
        build("restart_service", {})


def test_clear_temp_files_only_accepts_allowlisted_directories():
    assert build("clear_temp_files", {"directory": "/tmp"}).args["directory"] == "/tmp"

    with pytest.raises(ToolInputError, match="must be one of"):
        build("clear_temp_files", {"directory": "/etc"})
    with pytest.raises(ToolInputError, match="must be one of"):
        build("clear_temp_files", {"directory": "/home/alok"})


def test_numeric_bounds_are_enforced():
    with pytest.raises(ToolInputError, match=">= 1"):
        build("clear_temp_files", {"directory": "/tmp", "older_than_hours": 0})
    with pytest.raises(ToolInputError, match="<= 720"):
        build("clear_temp_files", {"directory": "/tmp", "older_than_hours": 10_000})


def test_a_bool_is_not_accepted_where_an_int_belongs():
    with pytest.raises(ToolInputError, match="must be int"):
        build("clear_temp_files", {"directory": "/tmp", "older_than_hours": True})


def test_defaults_are_applied_for_optional_arguments():
    assert build("clear_temp_files", {"directory": "/tmp"}).args["older_than_hours"] == 24


def test_destructive_actions_are_tiered_high_risk():
    assert ACTIONS["terminate_instance"].tier is Tier.HIGH_RISK
    assert not ACTIONS["terminate_instance"].idempotent
    assert "Irreversible" in ACTIONS["terminate_instance"].blast_radius


def test_every_action_declares_what_success_would_look_like():
    """Verification measures the expectation; an action without one could
    only ever be verified by its own exit code."""
    for name, spec in ACTIONS.items():
        assert spec.expects, f"{name} does not say what recovery looks like"
        assert spec.blast_radius, f"{name} does not say what it costs"


def test_the_catalogue_is_serialisable_for_the_api():
    entries = catalogue()
    assert {e["name"] for e in entries} == set(ACTIONS)
    assert all("tier" in e and "args" in e for e in entries)
    assert len(catalogue(tier=Tier.HIGH_RISK)) == 1
