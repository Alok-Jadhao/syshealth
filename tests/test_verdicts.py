"""Classification, summarisation and right-sizing.

The scenario tests at the bottom are the ones that matter: they assert that
each recorded fixture produces the verdict a human would give after looking at
the same numbers.
"""

import json
from pathlib import Path

import pytest

from syshealth.analysis import State, Thresholds, classify, summarise
from syshealth.catalog import Catalog
from syshealth.models import Interval
from syshealth.rightsize import Confidence, Policy, Sizing, evaluate

RUNS = Path(__file__).parent / "fixtures" / "runs"


def load_run(name: str) -> list[Interval]:
    path = RUNS / f"{name}.jsonl"
    return [
        Interval.from_dict(json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def sample(mem_some=0.0, mem_full=0.0, duration=2.0, **kw) -> Interval:
    return Interval(
        start_wall=0.0,
        end_wall=duration,
        duration_s=duration,
        stall={"memory.some": mem_some, "memory.full": mem_full},
        mem_used_pct=kw.get("mem_used_pct", 30.0),
        mem_naive_used_pct=kw.get("mem_naive_used_pct", 40.0),
        mem_total_kb=kw.get("mem_total_kb", 4 * 1024 * 1024),
        cpu_busy_pct=kw.get("cpu_busy_pct", 10.0),
        reclaim=kw.get("reclaim", {}),
        psi_available=kw.get("psi_available", True),
    )


# ------------------------------------------------------------ classify ----


@pytest.mark.parametrize(
    "stall,expected",
    [
        (0.0, State.HEALTHY),
        (0.99, State.HEALTHY),
        (1.0, State.DEGRADED),
        (9.9, State.DEGRADED),
        (10.0, State.SATURATED),
        (85.0, State.SATURATED),
    ],
)
def test_classify_boundaries(stall, expected):
    assert classify(sample(mem_some=stall)) is expected


def test_full_stall_escalates_on_its_own():
    """Low ``some`` but high ``full`` still means the box is in trouble."""
    assert classify(sample(mem_some=0.2, mem_full=3.0)) is State.SATURATED


def test_no_psi_is_unknown_not_healthy():
    """Reporting HEALTHY when we cannot measure would be the worst failure."""
    assert classify(sample(psi_available=False)) is State.UNKNOWN


def test_thresholds_are_configurable():
    strict = Thresholds(some_degraded=0.1, some_saturated=0.5)
    assert classify(sample(mem_some=0.6), thresholds=strict) is State.SATURATED


# ----------------------------------------------------------- summarise ----


def test_empty_run_is_not_claimed_healthy():
    result = summarise([])
    assert result.state is State.UNKNOWN
    assert result.psi_available is False


def test_summary_uses_percentile_not_max():
    """One transient spike must not condemn an otherwise clean run."""
    samples = [sample(mem_some=0.0) for _ in range(99)] + [sample(mem_some=90.0)]
    result = summarise(samples)
    memory = result.resources["memory"]
    assert memory.some_max == pytest.approx(90.0)
    assert memory.some_p95 < 1.0
    assert memory.state is State.HEALTHY


def test_stalled_seconds_accumulate():
    samples = [sample(mem_some=50.0, duration=2.0) for _ in range(10)]
    # 50% of 20 seconds
    assert summarise(samples).resources["memory"].stalled_seconds == pytest.approx(10.0)


def test_divergence_uses_the_naive_figure():
    samples = [sample(mem_some=0.1, mem_used_pct=25.0, mem_naive_used_pct=95.0)] * 10
    result = summarise(samples)
    assert result.divergence == pytest.approx(94.9, abs=0.1)


# ------------------------------------------------------------ evaluate ----


def test_refuses_verdict_without_psi():
    result = summarise([sample(psi_available=False) for _ in range(10)])
    verdict = evaluate(result, current_type="t3.medium")
    assert verdict.sizing is Sizing.INSUFFICIENT_DATA
    assert verdict.recommended is None


def test_refuses_verdict_on_too_few_samples():
    verdict = evaluate(summarise([sample() for _ in range(3)]), current_type="t3.medium")
    assert verdict.sizing is Sizing.INSUFFICIENT_DATA


def test_will_not_downsize_on_a_short_run():
    """A 60s quiet window is not evidence that a box can be shrunk."""
    samples = [sample(mem_some=0.0, duration=2.0) for _ in range(30)]
    verdict = evaluate(summarise(samples), current_type="t3.large")
    assert verdict.sizing is Sizing.RIGHT_SIZED
    assert verdict.confidence is Confidence.LOW


def test_unknown_instance_type_still_gives_a_recommendation():
    samples = [sample(mem_some=0.0, duration=10.0) for _ in range(60)]
    verdict = evaluate(summarise(samples), current_type="not-a-real-type")
    assert verdict.current is None
    assert verdict.recommended is not None
    assert any("instance-type" in c for c in verdict.caveats)


def test_largest_in_family_says_so_rather_than_inventing_a_size():
    samples = [sample(mem_some=40.0, duration=2.0) for _ in range(20)]
    verdict = evaluate(summarise(samples), current_type="t3.2xlarge")
    assert verdict.sizing is Sizing.UNDERSIZED
    assert any("largest size" in r for r in verdict.reasons)


def test_oom_kill_triggers_a_two_step_jump():
    samples = [
        sample(mem_some=30.0, duration=2.0, reclaim={"oom_kill": 1, "pgscan_direct": 900})
        for _ in range(20)
    ]
    verdict = evaluate(summarise(samples), current_type="t3.micro")
    assert verdict.recommended is not None
    assert verdict.recommended.name == "t3.medium"
    assert any("OOM" in r for r in verdict.reasons)


def test_headroom_policy_changes_the_recommendation():
    samples = [
        sample(mem_some=0.0, duration=10.0, mem_used_pct=45.0, mem_total_kb=8 * 1024 * 1024)
        for _ in range(60)
    ]
    result = summarise(samples)
    lean = evaluate(result, current_type="t3.large", policy=Policy(headroom=0.05))
    generous = evaluate(result, current_type="t3.large", policy=Policy(headroom=1.5))
    assert lean.recommended.ram_gb < generous.recommended.ram_gb


def test_every_verdict_carries_its_evidence():
    """A recommendation with no traceable evidence is not allowed to ship."""
    samples = [sample(mem_some=25.0, duration=2.0) for _ in range(20)]
    verdict = evaluate(summarise(samples), current_type="t3.micro")
    assert verdict.evidence
    assert verdict.reasons
    assert verdict.headline


# ----------------------------------------------------------- scenarios ----


def test_thrashing_run_is_undersized():
    """Reproduces the original t3.micro finding from the measurement rig."""
    verdict = evaluate(summarise(load_run("thrashing")), current_type="t3.micro")
    assert verdict.sizing is Sizing.UNDERSIZED
    assert verdict.confidence is Confidence.HIGH
    assert verdict.recommended.ram_gb >= 4
    assert verdict.monthly_delta_usd > 0


def test_cache_heavy_run_is_not_undersized():
    """The false alarm: 90%+ "used", zero saturation, must not say grow."""
    result = summarise(load_run("cache-heavy"))
    verdict = evaluate(result, current_type="t3.large")

    assert result.mem_naive_used_pct_max > 85
    assert result.resources["memory"].state is State.HEALTHY
    assert verdict.sizing is not Sizing.UNDERSIZED
    assert result.divergence > 80


def test_idle_run_is_oversized_and_saves_money():
    verdict = evaluate(summarise(load_run("idle-oversized")), current_type="t3.large")
    assert verdict.sizing is Sizing.OVERSIZED
    assert verdict.recommended.ram_gb < 8
    assert verdict.monthly_delta_usd < 0


# ------------------------------------------------------------- catalog ----


def test_catalog_step_up_stays_in_family():
    catalog = Catalog()
    assert catalog.step_up(catalog.get("t3.micro"), 1).name == "t3.small"
    assert catalog.step_up(catalog.get("t3.micro"), 2).name == "t3.medium"


def test_catalog_step_up_past_the_top_returns_none():
    catalog = Catalog()
    assert catalog.step_up(catalog.get("t3.2xlarge"), 1) is None


def test_smallest_with_prefers_cheapest_that_fits():
    catalog = Catalog()
    chosen = catalog.smallest_with(ram_gb=3.0, family="t3")
    assert chosen.name == "t3.medium"


def test_custom_catalog_overrides_builtin(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps([{"name": "custom.big", "vcpu": 8, "ram_gb": 64, "usd_per_hour": 1.0}])
    )
    catalog = Catalog.load(path)
    assert catalog.get("custom.big").usd_per_month == pytest.approx(730.0)
    assert catalog.get("t3.micro") is None


def test_bad_catalog_path_raises_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="could not read catalog"):
        Catalog.load(tmp_path / "nope.json")
