"""Readers for the kernel interfaces SysHealth measures.

Everything here takes a ``root`` so the whole stack can be pointed at a
directory of captured files instead of a live ``/proc``. That is what makes
this package testable on a laptop, in CI, and on kernels without PSI.

The important idea: PSI exposes both smoothed averages (``avg10/60/300``) and a
monotonic microsecond counter (``total``). The averages are convenient to
eyeball but cannot be aggregated or compared across intervals. The counter can:
sampling it twice yields the exact number of microseconds that tasks spent
stalled in between. All of SysHealth's verdicts are built on the counter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The three resources the kernel tracks pressure for.
RESOURCES = ("cpu", "memory", "io")

# ``some`` = at least one task stalled. ``full`` = every runnable task stalled,
# i.e. the machine got no useful work done at all. ``full`` is not meaningful
# for CPU and the kernel reports it as zero there.
SHARES = ("some", "full")


@dataclass(frozen=True)
class PressureLine:
    """One ``some``/``full`` line from a ``/proc/pressure/*`` file."""

    share: str
    avg10: float
    avg60: float
    avg300: float
    total_us: int


@dataclass(frozen=True)
class PressureReading:
    """All pressure lines for one resource at one instant."""

    resource: str
    some: PressureLine | None = None
    full: PressureLine | None = None

    def line(self, share: str) -> PressureLine | None:
        return self.some if share == "some" else self.full

    def total_us(self, share: str = "some") -> int:
        line = self.line(share)
        return line.total_us if line else 0


@dataclass(frozen=True)
class MemInfo:
    """The parts of ``/proc/meminfo`` needed to compute utilisation.

    Kept deliberately small. SysHealth reads utilisation only so it can be
    shown *next to* saturation and demonstrate how badly the two diverge.
    """

    total_kb: int = 0
    free_kb: int = 0
    available_kb: int = 0
    cached_kb: int = 0
    buffers_kb: int = 0
    swap_total_kb: int = 0
    swap_free_kb: int = 0

    @property
    def used_pct(self) -> float:
        """Utilisation the way a dashboard usually reports it.

        This is the number that lies. Linux fills unused RAM with page cache,
        so a healthy box routinely reads 85-95% "used" while stalling zero
        microseconds, and a thrashing box can read far less.
        """
        if self.total_kb <= 0:
            return 0.0
        return 100.0 * (self.total_kb - self.available_kb) / self.total_kb

    @property
    def naive_used_pct(self) -> float:
        """Utilisation the way ``total - free`` computes it.

        This counts page cache and buffers as "used", which is what most
        dashboards and many autoscaling rules do. It is the number that
        triggers false alarms, and SysHealth reports it purely so it can be
        shown next to the truth.
        """
        if self.total_kb <= 0:
            return 0.0
        return 100.0 * (self.total_kb - self.free_kb) / self.total_kb

    @property
    def swap_used_pct(self) -> float:
        if self.swap_total_kb <= 0:
            return 0.0
        return 100.0 * (self.swap_total_kb - self.swap_free_kb) / self.swap_total_kb


@dataclass(frozen=True)
class VmStat:
    """Reclaim counters. Monotonic, so useful as deltas only."""

    pgscan_direct: int = 0
    pgsteal_direct: int = 0
    pgscan_kswapd: int = 0
    pgsteal_kswapd: int = 0
    pgmajfault: int = 0
    pswpin: int = 0
    pswpout: int = 0
    oom_kill: int = 0

    FIELDS = (
        "pgscan_direct",
        "pgsteal_direct",
        "pgscan_kswapd",
        "pgsteal_kswapd",
        "pgmajfault",
        "pswpin",
        "pswpout",
        "oom_kill",
    )


@dataclass(frozen=True)
class CpuTimes:
    """Aggregate jiffies from the ``cpu`` line of ``/proc/stat``."""

    idle: int = 0
    total: int = 0


class ProcReader:
    """Reads kernel state from ``root`` (default ``/proc``).

    Every method fails soft and returns an empty/zero value rather than
    raising, because a monitoring agent must never die because one file it
    wanted was missing on this particular kernel.
    """

    def __init__(self, root: str | os.PathLike = "/proc") -> None:
        self.root = Path(root)

    # -- capability probing ------------------------------------------------

    def has_psi(self) -> bool:
        """Whether this kernel exposes PSI at all.

        Requires CONFIG_PSI=y and, on some distributions, the ``psi=1`` boot
        parameter. Kernel 4.20+.
        """
        return (self.root / "pressure" / "memory").exists()

    def missing_psi_reason(self) -> str:
        if not self.root.exists():
            return f"{self.root} does not exist"
        if not (self.root / "pressure").exists():
            return (
                "kernel has no /proc/pressure. PSI needs Linux 4.20+ with "
                "CONFIG_PSI=y; some distributions also require booting with "
                "psi=1 on the kernel command line."
            )
        return "/proc/pressure exists but /proc/pressure/memory is missing"

    # -- pressure ----------------------------------------------------------

    def read_pressure(self, resource: str) -> PressureReading:
        path = self.root / "pressure" / resource
        lines: dict[str, PressureLine] = {}
        try:
            text = path.read_text()
        except (OSError, ValueError):
            return PressureReading(resource=resource)

        for raw in text.splitlines():
            parsed = _parse_pressure_line(raw)
            if parsed:
                lines[parsed.share] = parsed

        return PressureReading(
            resource=resource,
            some=lines.get("some"),
            full=lines.get("full"),
        )

    def read_all_pressure(self) -> dict[str, PressureReading]:
        return {r: self.read_pressure(r) for r in RESOURCES}

    # -- memory ------------------------------------------------------------

    def read_meminfo(self) -> MemInfo:
        wanted = {
            "MemTotal": "total_kb",
            "MemFree": "free_kb",
            "MemAvailable": "available_kb",
            "Cached": "cached_kb",
            "Buffers": "buffers_kb",
            "SwapTotal": "swap_total_kb",
            "SwapFree": "swap_free_kb",
        }
        found: dict[str, int] = {}
        try:
            text = (self.root / "meminfo").read_text()
        except OSError:
            return MemInfo()

        for raw in text.splitlines():
            key, _, rest = raw.partition(":")
            field_name = wanted.get(key.strip())
            if not field_name:
                continue
            parts = rest.split()
            if parts and parts[0].isdigit():
                found[field_name] = int(parts[0])

        return MemInfo(**found)

    def read_vmstat(self) -> VmStat:
        found: dict[str, int] = {}
        try:
            text = (self.root / "vmstat").read_text()
        except OSError:
            return VmStat()

        for raw in text.splitlines():
            parts = raw.split()
            if len(parts) >= 2 and parts[0] in VmStat.FIELDS:
                try:
                    found[parts[0]] = int(parts[1])
                except ValueError:
                    continue

        return VmStat(**found)

    # -- cpu ---------------------------------------------------------------

    def read_cpu_times(self) -> CpuTimes:
        try:
            text = (self.root / "stat").read_text()
        except OSError:
            return CpuTimes()

        for raw in text.splitlines():
            if not raw.startswith("cpu "):
                continue
            parts = raw.split()[1:]
            try:
                values = [int(p) for p in parts]
            except ValueError:
                return CpuTimes()
            if len(values) < 5:
                return CpuTimes()
            # user nice system idle iowait irq softirq steal ...
            # iowait is famously unreliable; it is folded into total but the
            # CPU saturation verdict comes from PSI, not from this.
            return CpuTimes(idle=values[3], total=sum(values))

        return CpuTimes()

    # -- identity ----------------------------------------------------------

    def cpu_count(self) -> int:
        try:
            text = (self.root / "stat").read_text()
        except OSError:
            return os.cpu_count() or 1
        n = sum(
            1
            for line in text.splitlines()
            if line.startswith("cpu") and not line.startswith("cpu ")
        )
        return n or os.cpu_count() or 1


def _parse_pressure_line(raw: str) -> PressureLine | None:
    """Parse one line such as::

        some avg10=0.00 avg60=0.13 avg300=0.04 total=1234567

    Unknown or malformed lines return None rather than raising: kernels differ
    in which fields they emit and a monitoring agent should tolerate that.
    """
    parts = raw.split()
    if len(parts) < 2 or parts[0] not in SHARES:
        return None

    values: dict[str, float] = {}
    for token in parts[1:]:
        key, sep, value = token.partition("=")
        if not sep:
            continue
        try:
            values[key] = float(value)
        except ValueError:
            continue

    return PressureLine(
        share=parts[0],
        avg10=values.get("avg10", 0.0),
        avg60=values.get("avg60", 0.0),
        avg300=values.get("avg300", 0.0),
        total_us=int(values.get("total", 0)),
    )
