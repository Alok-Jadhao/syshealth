"""Sampler tests.

These drive the real sampling loop against a fake ``/proc`` that is rewritten
between reads, which is as close to a live PSI kernel as anything can get
without one. Sleeping is injected so the tests run instantly.
"""

import pytest

from syshealth.procfs import ProcReader
from syshealth.sampler import Sampler, collect


class MutatingProc:
    """A fake /proc whose PSI counters advance on every read."""

    def __init__(self, tmp_path, stall_us_per_tick: int = 200_000):
        self.root = tmp_path / "proc"
        (self.root / "pressure").mkdir(parents=True)
        self.total = 0
        self.step = stall_us_per_tick
        self.write()

    def write(self):
        (self.root / "pressure" / "memory").write_text(
            f"some avg10=1.00 avg60=1.00 avg300=1.00 total={self.total}\n"
            f"full avg10=0.00 avg60=0.00 avg300=0.00 total={self.total // 4}\n"
        )
        (self.root / "pressure" / "cpu").write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        (self.root / "pressure" / "io").write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        (self.root / "meminfo").write_text(
            "MemTotal:        2097152 kB\n"
            "MemFree:          104857 kB\n"
            "MemAvailable:    1048576 kB\n"
        )
        (self.root / "vmstat").write_text(f"pgscan_direct {self.total // 1000}\n")
        (self.root / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 0 1 1 0 0 0\n")

    def advance(self):
        self.total += self.step
        self.write()


@pytest.fixture
def fake(tmp_path):
    return MutatingProc(tmp_path)


def test_first_tick_returns_nothing(fake):
    """An interval needs two snapshots; the first read cannot produce one."""
    sampler = Sampler(ProcReader(fake.root))
    assert sampler.tick() is None
    fake.advance()
    assert sampler.tick() is not None


def test_stream_yields_requested_number_of_samples(fake):
    sampler = Sampler(ProcReader(fake.root))
    seen = []

    def fake_sleep(_seconds):
        fake.advance()

    for sample in sampler.stream(interval_s=1.0, duration_s=None, sleep=fake_sleep):
        seen.append(sample)
        if len(seen) >= 3:
            break

    assert len(seen) == 3
    assert all(s.some("memory") > 0 for s in seen)


def test_collect_returns_intervals(fake):
    samples = collect(
        ProcReader(fake.root),
        interval_s=0.0,
        duration_s=0.0,
        sleep=lambda _s: fake.advance(),
    )
    assert len(samples) >= 1
    assert samples[0].psi_available is True


def test_stall_reflects_the_advancing_counter(fake, monkeypatch):
    """0.2s of stall per 1s window should read as roughly 20%."""
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    sampler = Sampler(ProcReader(fake.root))
    sampler.tick()

    clock["t"] += 1.0
    fake.advance()  # 200_000 us == 0.2 s
    sample = sampler.tick()

    assert sample.some("memory") == pytest.approx(20.0, abs=0.01)
    assert sample.full("memory") == pytest.approx(5.0, abs=0.01)


def test_sampler_on_kernel_without_psi(tmp_path):
    """Must not crash; must not silently claim zero stall as healthy truth."""
    (tmp_path / "meminfo").write_text("MemTotal: 100 kB\n")
    sampler = Sampler(ProcReader(tmp_path))
    sampler.tick()
    sample = sampler.tick()
    assert sample is not None
    assert sample.psi_available is False
