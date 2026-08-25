"""The data model.

Two types matter:

``Snapshot`` is a raw instantaneous read of the kernel. It is mostly useless on
its own, because the interesting PSI field is a monotonic counter.

``Interval`` is the difference between two snapshots, and it is the unit of
analysis everywhere else in SysHealth. An Interval answers the only question
that matters: *during these N seconds, what fraction of the time was this
machine unable to make progress, and why?*
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .procfs import CpuTimes, MemInfo, PressureReading, VmStat

# Microseconds per second, spelled out because getting this wrong silently
# scales every verdict by a million.
US_PER_S = 1_000_000.0


@dataclass
class Snapshot:
    """One instantaneous read of every kernel source."""

    wall: float
    mono: float
    pressure: dict[str, PressureReading] = field(default_factory=dict)
    mem: MemInfo = field(default_factory=MemInfo)
    vmstat: VmStat = field(default_factory=VmStat)
    cpu: CpuTimes = field(default_factory=CpuTimes)


@dataclass
class Interval:
    """A measured window between two snapshots.

    ``stall`` maps ``"<resource>.<share>"`` to the percentage of wall-clock
    time spent stalled during the window, computed exactly from the PSI
    microsecond counters. ``"memory.some"`` of 8.4 means: for 8.4% of this
    window, at least one task was blocked waiting on memory.
    """

    start_wall: float
    end_wall: float
    duration_s: float

    stall: dict[str, float] = field(default_factory=dict)
    avg10: dict[str, float] = field(default_factory=dict)

    mem_used_pct: float = 0.0
    mem_naive_used_pct: float = 0.0
    mem_total_kb: int = 0
    swap_used_pct: float = 0.0
    cpu_busy_pct: float = 0.0

    reclaim: dict[str, int] = field(default_factory=dict)

    # Set when PSI is unavailable, so downstream code can degrade honestly
    # instead of reporting a confident zero.
    psi_available: bool = True

    # -- convenience accessors --------------------------------------------

    def some(self, resource: str) -> float:
        return self.stall.get(f"{resource}.some", 0.0)

    def full(self, resource: str) -> float:
        return self.stall.get(f"{resource}.full", 0.0)

    @property
    def worst_resource(self) -> str:
        """Whichever resource stalled the most in this window."""
        candidates = {r: self.some(r) for r in ("memory", "io", "cpu")}
        return max(candidates, key=lambda r: candidates[r])

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> Interval:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def diff(prev: Snapshot, curr: Snapshot) -> Interval:
    """Turn two snapshots into one measured Interval.

    Wall-clock duration comes from the monotonic clock, never from
    ``time.time()``, so that an NTP step mid-window cannot produce a negative
    duration and a nonsensical stall percentage.
    """
    duration = max(curr.mono - prev.mono, 1e-6)

    stall: dict[str, float] = {}
    avg10: dict[str, float] = {}

    # A dict of empty PressureReadings is not the same as having PSI. Testing
    # truthiness of the dict would report psi_available on a kernel with no
    # /proc/pressure at all, and the tool would then confidently call an
    # unmeasurable machine HEALTHY. Require at least one real parsed line.
    psi_available = any(
        reading.line("some") is not None for reading in curr.pressure.values()
    )

    for resource, reading in curr.pressure.items():
        before = prev.pressure.get(resource)
        for share in ("some", "full"):
            key = f"{resource}.{share}"
            line = reading.line(share)
            if line is None:
                continue
            avg10[key] = line.avg10

            prev_total = before.total_us(share) if before else 0
            delta_us = max(line.total_us - prev_total, 0)
            pct = 100.0 * (delta_us / US_PER_S) / duration
            # A counter reset (agent restart, container migration) can produce
            # a nonsense spike; clamp rather than emit an impossible number.
            stall[key] = min(pct, 100.0)

    cpu_busy = 0.0
    total_delta = curr.cpu.total - prev.cpu.total
    if total_delta > 0:
        idle_delta = max(curr.cpu.idle - prev.cpu.idle, 0)
        cpu_busy = 100.0 * (1.0 - idle_delta / total_delta)

    reclaim = {
        name: max(getattr(curr.vmstat, name) - getattr(prev.vmstat, name), 0)
        for name in VmStat.FIELDS
    }

    return Interval(
        start_wall=prev.wall,
        end_wall=curr.wall,
        duration_s=duration,
        stall=stall,
        avg10=avg10,
        mem_used_pct=curr.mem.used_pct,
        mem_naive_used_pct=curr.mem.naive_used_pct,
        mem_total_kb=curr.mem.total_kb,
        swap_used_pct=curr.mem.swap_used_pct,
        cpu_busy_pct=max(0.0, min(cpu_busy, 100.0)),
        reclaim=reclaim,
        psi_available=psi_available,
    )
