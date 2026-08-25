"""Turns a ``ProcReader`` into a stream of ``Interval`` measurements."""

from __future__ import annotations

import time
from collections.abc import Iterator

from .models import Interval, Snapshot, diff
from .procfs import ProcReader


class Sampler:
    """Produces one ``Interval`` per tick.

    The first tick produces nothing: an Interval needs two snapshots. Callers
    that iterate ``stream()`` never see this, but callers driving ``tick()``
    by hand must handle the initial ``None``.
    """

    def __init__(self, reader: ProcReader | None = None) -> None:
        self.reader = reader or ProcReader()
        self._prev: Snapshot | None = None

    def snapshot(self) -> Snapshot:
        return Snapshot(
            wall=time.time(),
            mono=time.monotonic(),
            pressure=self.reader.read_all_pressure(),
            mem=self.reader.read_meminfo(),
            vmstat=self.reader.read_vmstat(),
            cpu=self.reader.read_cpu_times(),
        )

    def tick(self) -> Interval | None:
        curr = self.snapshot()
        prev, self._prev = self._prev, curr
        if prev is None:
            return None
        return diff(prev, curr)

    def stream(
        self,
        interval_s: float = 2.0,
        duration_s: float | None = None,
        sleep=time.sleep,
    ) -> Iterator[Interval]:
        """Yield Intervals until ``duration_s`` elapses, or forever.

        Sleeps are scheduled against a monotonic deadline so that slow reads
        do not cause the sample period to drift.
        """
        self.tick()  # prime
        started = time.monotonic()
        next_at = started + interval_s

        while True:
            now = time.monotonic()
            if next_at > now:
                sleep(next_at - now)
            next_at += interval_s

            measured = self.tick()
            if measured is not None:
                yield measured

            if duration_s is not None and time.monotonic() - started >= duration_s:
                return


def collect(
    reader: ProcReader | None = None,
    interval_s: float = 2.0,
    duration_s: float = 30.0,
    sleep=time.sleep,
) -> list[Interval]:
    """Convenience: run a fixed-length measurement and return every Interval."""
    sampler = Sampler(reader)
    return list(sampler.stream(interval_s=interval_s, duration_s=duration_s, sleep=sleep))
