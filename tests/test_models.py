"""Tests for ``diff``, which turns two snapshots into a measurement.

This is the part of SysHealth that must be right. Everything downstream is
presentation; if the stall percentage here is wrong, every verdict is wrong.
"""

import pytest

from syshealth.models import Interval, Snapshot, diff
from syshealth.procfs import CpuTimes, MemInfo, PressureLine, PressureReading, VmStat


def snap(mono: float, memory_total_us: int, **kw) -> Snapshot:
    return Snapshot(
        wall=1_700_000_000.0 + mono,
        mono=mono,
        pressure={
            "memory": PressureReading(
                resource="memory",
                some=PressureLine("some", 0.0, 0.0, 0.0, memory_total_us),
                full=PressureLine("full", 0.0, 0.0, 0.0, kw.get("full_us", 0)),
            )
        },
        mem=kw.get("mem", MemInfo(total_kb=1_000_000, free_kb=100_000, available_kb=400_000)),
        vmstat=kw.get("vmstat", VmStat()),
        cpu=kw.get("cpu", CpuTimes()),
    )


def test_stall_percentage_from_counter_delta():
    """One full second of stall across a ten second window is 10%."""
    before = snap(0.0, 0)
    after = snap(10.0, 1_000_000)  # 1e6 microseconds == 1 second
    result = diff(before, after)
    assert result.some("memory") == pytest.approx(10.0)


def test_zero_delta_is_zero_stall():
    result = diff(snap(0.0, 5_000_000), snap(5.0, 5_000_000))
    assert result.some("memory") == 0.0


def test_full_stall_tracked_separately():
    result = diff(snap(0.0, 0, full_us=0), snap(10.0, 2_000_000, full_us=500_000))
    assert result.some("memory") == pytest.approx(20.0)
    assert result.full("memory") == pytest.approx(5.0)


def test_counter_reset_does_not_produce_negative_stall():
    """An agent restart or container migration can rewind the counter."""
    result = diff(snap(0.0, 9_000_000), snap(2.0, 10_000))
    assert result.some("memory") == 0.0


def test_stall_is_clamped_to_100_percent():
    """A nonsense delta must not yield an impossible percentage."""
    result = diff(snap(0.0, 0), snap(1.0, 999_000_000))
    assert result.some("memory") == 100.0


def test_duration_uses_monotonic_clock_not_wall():
    """An NTP step must not corrupt the measurement.

    Wall clock jumps backwards by an hour between the two snapshots while the
    monotonic clock advances normally. The stall figure must come out based on
    the monotonic delta of 10s.
    """
    before = snap(0.0, 0)
    after = snap(10.0, 1_000_000)
    after.wall = before.wall - 3600  # clock stepped backwards mid-window

    result = diff(before, after)
    assert result.duration_s == pytest.approx(10.0)
    assert result.some("memory") == pytest.approx(10.0)


def test_zero_duration_does_not_divide_by_zero():
    result = diff(snap(5.0, 0), snap(5.0, 1000))
    assert result.duration_s > 0


def test_cpu_busy_from_jiffies():
    before = snap(0.0, 0, cpu=CpuTimes(idle=100, total=200))
    after = snap(1.0, 0, cpu=CpuTimes(idle=150, total=300))
    # 50 idle jiffies out of 100 elapsed -> 50% busy
    assert diff(before, after).cpu_busy_pct == pytest.approx(50.0)


def test_reclaim_deltas_are_non_negative():
    before = snap(0.0, 0, vmstat=VmStat(pgscan_direct=500))
    after = snap(1.0, 0, vmstat=VmStat(pgscan_direct=100))
    assert diff(before, after).reclaim["pgscan_direct"] == 0


def test_roundtrips_through_json():
    original = diff(snap(0.0, 0), snap(2.0, 400_000))
    restored = Interval.from_dict(original.to_dict())
    assert restored.some("memory") == pytest.approx(original.some("memory"))
    assert restored.duration_s == pytest.approx(original.duration_s)


def test_from_dict_ignores_unknown_keys():
    """Forward compatibility: a newer agent may send fields we do not know."""
    restored = Interval.from_dict(
        {"start_wall": 1.0, "end_wall": 2.0, "duration_s": 1.0, "future_field": "?"}
    )
    assert restored.duration_s == 1.0


def test_worst_resource_picks_the_biggest_staller():
    sample = Interval(
        start_wall=0,
        end_wall=1,
        duration_s=1,
        stall={"memory.some": 1.0, "io.some": 7.0, "cpu.some": 3.0},
    )
    assert sample.worst_resource == "io"
