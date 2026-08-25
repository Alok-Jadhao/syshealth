"""Parser tests, run against a captured /proc tree.

The point of the ``root`` argument on ProcReader is exactly this: none of these
tests need a PSI-capable kernel, so they pass on a laptop, in a container, and
on a macOS CI runner.
"""

from pathlib import Path

import pytest

from syshealth.procfs import ProcReader, _parse_pressure_line

FIXTURE = Path(__file__).parent / "fixtures" / "proc"


@pytest.fixture
def reader() -> ProcReader:
    return ProcReader(FIXTURE)


def test_detects_psi(reader):
    assert reader.has_psi() is True


def test_missing_psi_is_reported_not_raised(tmp_path):
    empty = ProcReader(tmp_path)
    assert empty.has_psi() is False
    assert "pressure" in empty.missing_psi_reason()


def test_reads_memory_pressure(reader):
    memory = reader.read_pressure("memory")
    assert memory.some is not None
    assert memory.some.avg10 == pytest.approx(4.21)
    assert memory.some.total_us == 91234567
    assert memory.full is not None
    assert memory.full.total_us == 20111222


def test_cpu_has_no_full_line(reader):
    """The kernel does not report a meaningful ``full`` for CPU."""
    cpu = reader.read_pressure("cpu")
    assert cpu.some is not None
    assert cpu.full is None
    assert cpu.total_us("full") == 0


def test_unknown_resource_returns_empty(reader):
    empty = reader.read_pressure("does-not-exist")
    assert empty.some is None
    assert empty.total_us("some") == 0


def test_reads_meminfo(reader):
    mem = reader.read_meminfo()
    assert mem.total_kb == 4030524
    assert mem.available_kb == 980120
    assert mem.free_kb == 210332


def test_working_set_and_naive_utilisation_differ(reader):
    """The whole argument of the project, asserted as a unit test.

    The used/free rule counts page cache as used and reads far higher than the
    MemAvailable-based working set on the same machine at the same instant.
    """
    mem = reader.read_meminfo()
    assert mem.naive_used_pct > 90
    assert mem.used_pct < 80
    assert mem.naive_used_pct - mem.used_pct > 15


def test_reads_vmstat(reader):
    vmstat = reader.read_vmstat()
    assert vmstat.pgscan_direct == 120455
    assert vmstat.pgmajfault == 4471
    assert vmstat.oom_kill == 0


def test_reads_cpu_times(reader):
    cpu = reader.read_cpu_times()
    assert cpu.idle == 3901220
    assert cpu.total > cpu.idle


def test_counts_cpus(reader):
    assert reader.cpu_count() == 2


def test_missing_files_yield_zeros_not_exceptions(tmp_path):
    empty = ProcReader(tmp_path)
    assert empty.read_meminfo().total_kb == 0
    assert empty.read_vmstat().pgscan_direct == 0
    assert empty.read_cpu_times().total == 0


@pytest.mark.parametrize(
    "line",
    [
        "",
        "garbage",
        "some",
        "avg10=1.0 total=5",
        "sideways avg10=1.0 total=5",
    ],
)
def test_malformed_pressure_lines_return_none(line):
    assert _parse_pressure_line(line) is None


def test_partial_pressure_line_is_tolerated():
    """Kernels differ in which fields they emit; missing ones default to zero."""
    parsed = _parse_pressure_line("some avg10=2.5 total=42")
    assert parsed is not None
    assert parsed.avg10 == 2.5
    assert parsed.avg60 == 0.0
    assert parsed.total_us == 42
