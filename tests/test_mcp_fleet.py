"""Tests for the fleet tool layer.

A store is built from the recorded runs, so a "fleet" here is the three
scenarios the project already reasons about, wearing node names. No SDK, no
kernel, no network.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from syshealth.analysis import State
from syshealth.mcp import ToolInputError, build_fleet_tools, load_run
from syshealth.store import Store

RUNS = Path(__file__).parent / "fixtures" / "runs"

# node name -> (recorded run, instance type it was recorded on)
FLEET = {
    "web-01": ("thrashing", "t3.micro"),
    "cache-01": ("cache-heavy", "t3.large"),
    "batch-01": ("idle-oversized", "t3.2xlarge"),
}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """A fleet database with three nodes, all pushing as of now."""
    db = Store(tmp_path / "fleet.db")
    now = time.time()
    for node, (run, instance_type) in FLEET.items():
        samples = load_run(RUNS / f"{run}.jsonl")
        for offset, sample in enumerate(samples):
            db.record(
                node=node,
                payload=sample.to_dict(),
                instance_type=instance_type,
                address="10.0.0.1",
                now=now - (len(samples) - offset) * 2.0,
            )
    return db


@pytest.fixture
def tools(store: Store) -> dict:
    return build_fleet_tools(store)


# ------------------------------------------------------------- list_nodes --


def test_list_nodes_returns_the_whole_fleet(tools: dict):
    result = tools["list_nodes"].handler()

    assert result["count"] == 3
    assert {n["node"] for n in result["nodes"]} == set(FLEET)
    assert {n["instance_type"] for n in result["nodes"]} == {
        t for _, t in FLEET.values()
    }


def test_list_nodes_on_an_empty_fleet_says_so(tmp_path: Path):
    result = build_fleet_tools(Store(tmp_path / "empty.db"))["list_nodes"].handler()

    assert result["count"] == 0
    assert result["nodes"] == []


# --------------------------------------------------------- get_node_health --


def test_a_thrashing_node_is_saturated_on_memory(tools: dict):
    health = tools["get_node_health"].handler(node="web-01")

    assert health["state"] == State.SATURATED.value
    assert health["bottleneck"] == "memory"
    assert health["resources"]["memory"]["some_p95_pct"] >= 10.0
    assert health["reclaim"]["direct_reclaim_total"] > 0


def test_a_cache_heavy_node_shows_no_memory_problem_despite_high_utilisation(tools: dict):
    """The false alarm, seen through the fleet rather than one window.

    The claim being pinned is specifically about *memory*: 90%+ "used" and
    effectively no stalling. This node is DEGRADED overall on io, which is a
    separate and true fact — the point is that nothing here would justify
    provisioning more RAM.
    """
    health = tools["get_node_health"].handler(node="cache-01")

    assert health["resources"]["memory"]["state"] == State.HEALTHY.value
    assert health["resources"]["memory"]["some_p95_pct"] < 1.0
    assert health["utilisation"]["naive_used_pct_max"] > 90.0
    assert health["divergence_pct_points"] > 50.0
    assert health["bottleneck"] != "memory"


def test_history_beats_a_single_window(tools: dict):
    """The reason Phase 2 exists.

    A single window of the thrashing run can read calm — that is exactly what
    the Phase 1 tools showed. The p95 over the node's stored history cannot.
    """
    health = tools["get_node_health"].handler(node="web-01")
    memory = health["resources"]["memory"]

    assert health["window"]["samples"] > 50
    assert memory["some_p50_pct"] < memory["some_max_pct"]
    assert health["state"] == State.SATURATED.value
    assert f"p{95}" in health["note"]


def test_max_samples_bounds_the_history_read(tools: dict):
    health = tools["get_node_health"].handler(node="web-01", max_samples=10)
    assert health["window"]["samples"] == 10


@pytest.mark.parametrize("bad", [0, -1, 5000])
def test_max_samples_outside_the_bounds_is_rejected(tools: dict, bad: int):
    with pytest.raises(ToolInputError, match="max_samples must be between"):
        tools["get_node_health"].handler(node="web-01", max_samples=bad)


@pytest.mark.parametrize("bad", ["10", 1.5, True])
def test_max_samples_of_the_wrong_type_is_rejected(tools: dict, bad):
    with pytest.raises(ToolInputError, match="max_samples must be an integer"):
        tools["get_node_health"].handler(node="web-01", max_samples=bad)


def test_an_unknown_node_is_refused_with_the_known_ones(tools: dict):
    """A model that guessed a name needs to know what to guess next."""
    with pytest.raises(ToolInputError, match="unknown node") as exc:
        tools["get_node_health"].handler(node="web-99")

    message = str(exc.value)
    assert all(node in message for node in FLEET)


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
def test_a_missing_node_name_is_refused(tools: dict, bad):
    with pytest.raises(ToolInputError, match="node must be a non-empty string"):
        tools["get_node_health"].handler(node=bad)


# -------------------------------------------------------- get_node_verdict --


def test_the_thrashing_node_is_told_to_grow(tools: dict):
    verdict = tools["get_node_verdict"].handler(node="web-01")

    assert verdict["sizing"] == "UNDERSIZED"
    assert verdict["current"]["name"] == "t3.micro"
    assert verdict["recommended"]["name"] != "t3.micro"
    assert verdict["monthly_delta_usd"] > 0


def test_a_verdict_keeps_its_reasoning_separate_from_its_conclusion(tools: dict):
    """§6: a diagnosis is only checkable if the evidence travels with it."""
    verdict = tools["get_node_verdict"].handler(node="web-01")

    assert verdict["reasons"], "must say why it decided"
    assert verdict["evidence"], "must say what it decided from"
    assert set(verdict) >= {"reasons", "evidence", "caveats", "confidence"}


def test_an_oversized_node_is_told_to_shrink_and_says_by_how_much(tools: dict):
    verdict = tools["get_node_verdict"].handler(node="batch-01")

    assert verdict["sizing"] == "OVERSIZED"
    assert verdict["monthly_delta_usd"] < 0, "shrinking should save money"
    assert verdict["annual_delta_usd"] == pytest.approx(
        verdict["monthly_delta_usd"] * 12, rel=1e-3
    )


# ------------------------------------------------------- get_fleet_summary --


def test_the_fleet_summary_counts_states_and_ranks_what_needs_attention(tools: dict):
    fleet = tools["get_fleet_summary"].handler()

    assert fleet["nodes"] == 3
    assert fleet["by_state"][State.SATURATED.value] == 1
    assert fleet["by_state"][State.DEGRADED.value] == 1
    assert fleet["by_state"][State.HEALTHY.value] == 1

    # Worst stall first, so the node to investigate is at the top.
    attention = fleet["needs_attention"]
    assert [n["node"] for n in attention] == ["web-01", "cache-01"]
    assert attention[0]["bottleneck"] == "memory"
    assert attention[0]["some_p95_pct"] > attention[1]["some_p95_pct"]


def test_the_fleet_summary_totals_the_money(tools: dict):
    fleet = tools["get_fleet_summary"].handler()

    assert fleet["by_sizing"]["UNDERSIZED"] == 1
    assert fleet["by_sizing"]["OVERSIZED"] >= 1
    assert fleet["annual_delta_usd"] == pytest.approx(
        fleet["monthly_delta_usd"] * 12, rel=1e-3
    )


def test_a_node_with_no_samples_is_reported_not_silently_dropped(store: Store, tmp_path: Path):
    """Retention can expire a node's samples while the node row remains. That
    must not look like a healthy fleet member."""
    store.record(node="ghost-01", payload={}, instance_type="t3.small")
    store._conn.execute("DELETE FROM samples WHERE node = 'ghost-01'")
    store._conn.commit()

    fleet = build_fleet_tools(store)["get_fleet_summary"].handler()

    assert "ghost-01" in fleet["no_samples"]
    assert fleet["nodes"] == 4


# ------------------------------------------------------------- read-only ---


def test_a_read_only_store_cannot_be_written_to(tmp_path: Path, store: Store):
    """§11's first rule, made structural rather than a promise."""
    reader = Store(tmp_path / "fleet.db", read_only=True)

    assert reader.count() > 0
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.record(node="attacker", payload={}, instance_type="t3.nano")


def test_a_read_only_store_refuses_a_path_that_does_not_exist(tmp_path: Path):
    """Otherwise a mistyped --db silently becomes an empty fleet."""
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "typo.db", read_only=True)


def test_the_fleet_tools_work_against_a_read_only_store(tmp_path: Path, store: Store):
    tools = build_fleet_tools(Store(tmp_path / "fleet.db", read_only=True))

    assert tools["list_nodes"].handler()["count"] == 3
    assert tools["get_node_health"].handler(node="web-01")["state"] == State.SATURATED.value
