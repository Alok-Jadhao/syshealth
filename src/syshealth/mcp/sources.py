"""Where a tool's measurements come from.

One method, two implementations. ``LiveSource`` measures the machine the
server runs on. ``ReplaySource`` replays a run recorded earlier, which is what
makes the tool layer exercisable on a laptop with no PSI kernel — the same
property the test suite already enforces for everything else.

Replay is not only a testing convenience. A static ``/proc`` fixture cannot
produce a non-zero stall, because PSI's ``total=`` is a counter and reading
the same file twice yields a delta of zero. So fixtures alone can only ever
demonstrate a healthy machine. Recorded runs are what let the interesting
cases — a thrashing box, a cache-heavy false alarm — be demonstrated at all.

Every source names itself and every tool response carries that name. An agent
reasoning over a replayed run must never be able to mistake it for a live
machine.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from ..models import Interval
from ..procfs import ProcReader
from ..sampler import Sampler


class MetricSource(Protocol):
    """Anything that can produce one measured ``Interval`` on demand."""

    @property
    def name(self) -> str:
        """Short identifier, reported in every tool response."""
        ...

    def measure(self, window_s: float) -> Interval:
        """Return one Interval. May block for up to ``window_s`` seconds."""
        ...


class LiveSource:
    """Measures the running machine over a fresh window per call.

    A new ``Sampler`` per call rather than one kept warm across calls: tool
    invocations can be minutes apart, and a stall percentage computed against
    a snapshot taken during some earlier, unrelated request would be an
    average over dead time dressed up as a measurement.
    """

    def __init__(self, proc_root: str = "/proc", sleep=time.sleep) -> None:
        self.reader = ProcReader(proc_root)
        self.proc_root = str(proc_root)
        self._sleep = sleep

    @property
    def name(self) -> str:
        return f"live:{self.proc_root}"

    @property
    def psi_available(self) -> bool:
        return self.reader.has_psi()

    def measure(self, window_s: float) -> Interval:
        sampler = Sampler(self.reader)
        sampler.tick()  # prime; an Interval needs two snapshots
        self._sleep(window_s)
        measured = sampler.tick()
        if measured is None:  # pragma: no cover - second tick always measures
            raise RuntimeError("sampler produced no interval")
        return measured


class ReplaySource:
    """Replays recorded Intervals in order, then repeats.

    ``window_s`` is accepted and validated for a uniform tool contract but
    cannot change what a recording contains; the Interval's own recorded
    duration is what gets reported back as the measured window.
    """

    def __init__(self, samples: list[Interval], label: str = "recorded") -> None:
        if not samples:
            raise ValueError("replay source needs at least one sample")
        self.samples = samples
        self.label = label
        self._index = 0

    @property
    def name(self) -> str:
        return f"replay:{self.label}"

    @property
    def psi_available(self) -> bool:
        return all(s.psi_available for s in self.samples)

    def measure(self, window_s: float) -> Interval:
        sample = self.samples[self._index % len(self.samples)]
        self._index += 1
        return sample


def load_run(path: str | Path) -> list[Interval]:
    """Read a JSONL run written by ``syshealth profile --save``."""
    source = Path(path)
    samples: list[Interval] = []
    for raw in source.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            samples.append(Interval.from_dict(json.loads(raw)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{source}: unusable sample: {exc}") from exc

    if not samples:
        raise ValueError(f"{source}: no samples in file")
    return samples
