"""Tests for the MCP tool layer.

Every test here runs against recorded data or the captured /proc fixtures, so
the suite still passes on a machine with no PSI kernel and no MCP SDK
installed. That is the same constraint the rest of the suite is held to, and
it is what makes the tool layer developable on any laptop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syshealth.analysis import State, Thresholds
from syshealth.mcp import LiveSource, ReplaySource, Tier, build_tools, load_run

FIXTURES = Path(__file__).parent / "fixtures"
RUNS = FIXTURES / "runs"


@pytest.fixture
def thrashing() -> ReplaySource:
    return ReplaySource(load_run(RUNS / "thrashing.jsonl"), label="thrashing")


@pytest.fixture
def cache_heavy() -> ReplaySource:
    return ReplaySource(load_run(RUNS / "cache-heavy.jsonl"), label="cache-heavy")


# ----------------------------------------------------------------- sources --


def test_load_run_reads_recorded_samples():
    samples = load_run(RUNS / "thrashing.jsonl")
    assert samples
    assert all(s.duration_s > 0 for s in samples)


def test_load_run_rejects_an_empty_file(tmp_path: Path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    with pytest.raises(ValueError, match="no samples"):
        load_run(empty)


def test_replay_source_advances_then_repeats():
    samples = load_run(RUNS / "thrashing.jsonl")
    source = ReplaySource(samples[:2], label="pair")

    first = source.measure(1.0)
    second = source.measure(1.0)
    third = source.measure(1.0)

    assert first is samples[0]
    assert second is samples[1]
    assert third is samples[0], "should cycle rather than run out"


def test_replay_source_refuses_to_be_empty():
    with pytest.raises(ValueError):
        ReplaySource([], label="nothing")


def test_live_source_measures_without_sleeping_for_real():
    """The fixtures are static, so both snapshots read identical counters.

    That means a stall of exactly zero — which is the point: it proves the
    plumbing runs end to end, and it is also why ReplaySource exists, since
    no static fixture can ever demonstrate a saturated machine.
    """
    slept: list[float] = []
    source = LiveSource(FIXTURES / "proc", sleep=slept.append)

    sample = source.measure(2.0)

    assert slept == [2.0], "must wait for the window it claims to measure"
    assert sample.psi_available is True
    assert sample.some("memory") == 0.0
    assert sample.mem_total_kb > 0


def test_source_names_distinguish_live_from_replay(thrashing: ReplaySource):
    assert thrashing.name == "replay:thrashing"
    assert LiveSource("/proc").name == "live:/proc"


# ------------------------------------------------------------- tool wiring --


def test_every_tool_is_read_only_in_phase_one(thrashing: ReplaySource):
    tools = build_tools(thrashing)

    assert set(tools) == {
        "get_health",
        "get_memory_pressure",
        "get_cpu_pressure",
        "get_reclaim_activity",
    }
    assert all(tool.tier is Tier.READ_ONLY for tool in tools.values())
    assert all(tool.description.strip() for tool in tools.values())


def test_every_response_names_its_source(thrashing: ReplaySource):
    for name, tool in build_tools(thrashing).items():
        result = tool.handler()
        assert result["source"] == "replay:thrashing", name
        assert result["measured_s"] > 0, name
        assert result["psi_available"] is True, name


@pytest.mark.parametrize("bad", [0.0, 0.1, 31.0, -5.0, 1e9])
def test_window_outside_the_bounds_is_rejected(thrashing: ReplaySource, bad: float):
    handler = build_tools(thrashing)["get_health"].handler
    with pytest.raises(ValueError, match="window_s must be between"):
        handler(window_s=bad)


@pytest.mark.parametrize("bad", ["2", None, True])
def test_window_of_the_wrong_type_is_rejected(thrashing: ReplaySource, bad):
    handler = build_tools(thrashing)["get_health"].handler
    with pytest.raises(ValueError, match="window_s must be a number"):
        handler(window_s=bad)


# ------------------------------------------------------------- diagnostics --


def test_thrashing_run_reports_a_saturated_memory_bottleneck(thrashing: ReplaySource):
    """The scenario modelled on the project's real t3.micro result."""
    tools = build_tools(thrashing)

    states = {tools["get_health"].handler()["state"] for _ in range(20)}

    assert State.SATURATED.value in states
    health = tools["get_health"].handler(window_s=5.0)
    assert health["resources"]["memory"]["some_stall_pct"] > 0


def test_cache_heavy_run_is_high_utilisation_and_no_stalling(cache_heavy: ReplaySource):
    """The false alarm: 90%+ "used", nothing actually waiting.

    This is the case the tool descriptions exist to stop a model misreading,
    so it is worth asserting that the payload really does contain both halves
    of the contradiction.
    """
    memory = build_tools(cache_heavy)["get_memory_pressure"].handler()

    assert memory["utilisation"]["naive_used_pct"] > 90.0
    assert memory["saturation"]["some_stall_pct"] < Thresholds().some_degraded
    assert memory["saturation"]["state"] == State.HEALTHY.value
    assert memory["divergence_pct_points"] > 25.0
    assert "page cache" in memory["divergence_note"].lower()


def test_no_window_of_a_thrashing_run_is_called_harmless():
    """The regression that motivated ``_quiet_high_utilisation``.

    A thrashing run contains a couple of windows where PSI memory.some reads
    zero — the machine was already reclaiming hard and swapping pages back in,
    but nothing had blocked long enough inside that particular window. Gating
    the note on stall alone attached "not evidence of a memory problem" to a
    box in genuine trouble. Every window in the run is checked, not a sample.
    """
    samples = load_run(RUNS / "thrashing.jsonl")
    handler = build_tools(ReplaySource(samples, label="thrashing"))[
        "get_memory_pressure"
    ].handler

    results = [handler() for _ in samples]
    quiet = [r for r in results if r["saturation"]["some_stall_pct"] < Thresholds().some_degraded]

    assert quiet, "run should contain windows PSI reads as quiet"
    assert all("divergence_note" not in r for r in results)


def test_the_note_survives_where_it_is_actually_true(cache_heavy: ReplaySource):
    """Suppressing the note everywhere would be a cheap way to pass the test
    above. It must still fire on the genuine false alarm."""
    samples = load_run(RUNS / "cache-heavy.jsonl")
    handler = build_tools(ReplaySource(samples, label="cache-heavy")).__getitem__(
        "get_memory_pressure"
    ).handler

    assert all("divergence_note" in handler() for _ in samples)


def test_memory_tool_reports_both_utilisation_figures(cache_heavy: ReplaySource):
    memory = build_tools(cache_heavy)["get_memory_pressure"].handler()

    assert "working_set_pct" in memory["utilisation"]
    assert "naive_used_pct" in memory["utilisation"]
    assert memory["utilisation"]["naive_used_pct"] >= memory["utilisation"]["working_set_pct"]
    assert memory["saturation"]["stalled_seconds"] >= 0


def test_cpu_tool_separates_busy_from_stalled(thrashing: ReplaySource):
    cpu = build_tools(thrashing)["get_cpu_pressure"].handler()

    assert 0.0 <= cpu["utilisation"]["busy_pct"] <= 100.0
    assert cpu["saturation"]["some_stall_pct"] >= 0.0
    assert cpu["saturation"]["state"] in {s.value for s in State}


def test_reclaim_tool_flags_sustained_direct_reclaim(thrashing: ReplaySource):
    handler = build_tools(thrashing)["get_reclaim_activity"].handler
    results = [handler() for _ in range(20)]

    assert any(r["direct_reclaim_per_s"] > 0 for r in results)
    assert any(r["sustained_direct_reclaim"] for r in results)
    assert all("pgscan_direct" in r["counts"] for r in results)


def test_no_psi_is_reported_as_unknown_not_healthy(tmp_path: Path):
    """A kernel with no /proc/pressure must never look like a healthy one."""
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 500 kB\n")

    health = build_tools(LiveSource(tmp_path, sleep=lambda _: None))["get_health"].handler()

    assert health["psi_available"] is False
    assert health["state"] == State.UNKNOWN.value
    assert "UNKNOWN" in health["caveat"]
