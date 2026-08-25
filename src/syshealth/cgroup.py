"""Reading pressure for one cgroup instead of the whole machine.

The limitation this removes: ``/proc/pressure/*`` is host-wide. Inside a
container it either does not exist or reports the host's numbers, so "which
container is saturating this box?" has no answer from procfs alone — and that
question is the whole point of a Docker-based chaos environment.

cgroup v2 exposes the same PSI counters per control group
(``memory.pressure``, ``cpu.pressure``, ``io.pressure``), in exactly the same
format. So the reader here is deliberately shaped like ``ProcReader``: same
methods, same return types, and therefore usable by ``Sampler``, ``diff``,
``summarise`` and every tool without any of them knowing the difference.

Two things are genuinely approximated, and it is better to say so than to
paper over them:

* ``MemInfo.total_kb`` is the cgroup's limit. An unlimited cgroup (``max``)
  has no meaningful total, so it falls back to the host's, and utilisation for
  that cgroup is then a host figure. Saturation is unaffected — it is measured,
  not derived.
* cgroup ``memory.stat`` reports ``pgscan``/``pgsteal`` split by source and
  does not expose every counter ``/proc/vmstat`` does. Missing ones read zero
  rather than being invented.
"""

from __future__ import annotations

import os
from pathlib import Path

from .procfs import (
    RESOURCES,
    CpuTimes,
    MemInfo,
    PressureReading,
    ProcReader,
    VmStat,
    _parse_pressure_line,
)

CGROUP_ROOT = Path("/sys/fs/cgroup")

# Microseconds of CPU time -> the jiffy-like units CpuTimes carries. The
# absolute unit does not matter: diff() only ever takes a ratio of deltas.
US_PER_TICK = 1


class CgroupReader:
    """Reads PSI and memory for one cgroup v2 control group.

    ``root`` is the cgroup directory — ``/sys/fs/cgroup`` for the whole
    machine, or a container's own path underneath it. Interface-compatible
    with ``ProcReader`` on purpose.
    """

    def __init__(
        self,
        root: str | os.PathLike = CGROUP_ROOT,
        proc_root: str | os.PathLike = "/proc",
    ) -> None:
        self.root = Path(root)
        # Only for the fallbacks that a cgroup genuinely cannot answer.
        self._host = ProcReader(proc_root)

    # -- capability probing -------------------------------------------------

    def has_psi(self) -> bool:
        return (self.root / "memory.pressure").exists()

    def missing_psi_reason(self) -> str:
        if not self.root.exists():
            return f"{self.root} does not exist — is this cgroup v2?"
        if (self.root / "cgroup.controllers").exists():
            return (
                f"{self.root} is a cgroup but exposes no memory.pressure. PSI "
                "per-cgroup needs Linux 4.20+ with CONFIG_PSI=y and cgroup v2 "
                "(unified hierarchy)."
            )
        return f"{self.root} does not look like a cgroup v2 directory"

    # -- pressure -----------------------------------------------------------

    def read_pressure(self, resource: str) -> PressureReading:
        path = self.root / f"{resource}.pressure"
        lines = {}
        try:
            text = path.read_text()
        except (OSError, ValueError):
            return PressureReading(resource=resource)

        for raw in text.splitlines():
            parsed = _parse_pressure_line(raw)
            if parsed:
                lines[parsed.share] = parsed

        return PressureReading(
            resource=resource, some=lines.get("some"), full=lines.get("full")
        )

    def read_all_pressure(self) -> dict[str, PressureReading]:
        return {r: self.read_pressure(r) for r in RESOURCES}

    # -- memory -------------------------------------------------------------

    def read_meminfo(self) -> MemInfo:
        current = self._number("memory.current")
        limit = self._limit()
        cached = self._stat_value("memory.stat", "file")
        swap_current = self._number("memory.swap.current")
        swap_limit = self._limit("memory.swap.max")

        if limit <= 0:
            # No limit on this cgroup: utilisation has no cgroup-local meaning,
            # so report the host's rather than inventing a denominator.
            return self._host.read_meminfo()

        total_kb = limit // 1024
        used_kb = current // 1024
        cached_kb = cached // 1024

        return MemInfo(
            total_kb=total_kb,
            # MemAvailable's analogue: the limit minus what is not reclaimable.
            available_kb=max(total_kb - (used_kb - cached_kb), 0),
            free_kb=max(total_kb - used_kb, 0),
            cached_kb=cached_kb,
            buffers_kb=0,
            swap_total_kb=(swap_limit // 1024) if swap_limit > 0 else 0,
            swap_free_kb=max((swap_limit - swap_current) // 1024, 0)
            if swap_limit > 0
            else 0,
        )

    def read_vmstat(self) -> VmStat:
        stat = self._stat("memory.stat")
        events = self._stat("memory.events")
        return VmStat(
            pgscan_direct=stat.get("pgscan_direct", 0),
            pgsteal_direct=stat.get("pgsteal_direct", 0),
            pgscan_kswapd=stat.get("pgscan_kswapd", 0),
            pgsteal_kswapd=stat.get("pgsteal_kswapd", 0),
            pgmajfault=stat.get("pgmajfault", 0),
            pswpin=stat.get("pswpin", 0),
            pswpout=stat.get("pswpout", 0),
            oom_kill=events.get("oom_kill", 0),
        )

    # -- cpu ----------------------------------------------------------------

    def read_cpu_times(self) -> CpuTimes:
        """Busy and total for this cgroup.

        cgroups report time used, not time idle, so "total" is wall-clock
        across the machine's CPUs and idle is the remainder. Only the ratio of
        deltas is ever used, so the units cancel.
        """
        stat = self._stat("cpu.stat")
        used = stat.get("usage_usec", 0)
        if not used:
            return CpuTimes()

        cpus = os.cpu_count() or 1
        # A monotonic wall-clock in the same units, scaled by core count, so
        # busy% comes out as a share of available CPU.
        wall = int(time_us() * cpus)
        return CpuTimes(idle=max(wall - used, 0), total=wall)

    def cpu_count(self) -> int:
        return self._host.cpu_count()

    # -- parsing ------------------------------------------------------------

    def _number(self, name: str) -> int:
        try:
            return int((self.root / name).read_text().strip())
        except (OSError, ValueError):
            return 0

    def _limit(self, name: str = "memory.max") -> int:
        try:
            raw = (self.root / name).read_text().strip()
        except OSError:
            return 0
        if raw == "max":
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def _stat(self, name: str) -> dict[str, int]:
        found: dict[str, int] = {}
        try:
            text = (self.root / name).read_text()
        except OSError:
            return found
        for raw in text.splitlines():
            parts = raw.split()
            if len(parts) >= 2:
                try:
                    found[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        return found

    def _stat_value(self, name: str, key: str) -> int:
        return self._stat(name).get(key, 0)


def time_us() -> int:
    import time

    return int(time.monotonic() * 1_000_000)


def for_container(container_id: str, root: str | os.PathLike = CGROUP_ROOT) -> CgroupReader:
    """Find the cgroup for a Docker container id.

    Docker's cgroup path varies by driver and by whether the daemon is
    rootless, so several known layouts are tried and the first that actually
    exposes ``memory.pressure`` wins. Nothing is guessed: if none of them
    exist, this raises rather than silently measuring the host and reporting
    it as the container.
    """
    base = Path(root)
    candidates = [
        base / "system.slice" / f"docker-{container_id}.scope",
        base / "docker" / container_id,
        base / "kubepods" / f"docker-{container_id}.scope",
        base / f"docker-{container_id}.scope",
    ]
    for path in candidates:
        if (path / "memory.pressure").exists():
            return CgroupReader(path)

    raise FileNotFoundError(
        f"no cgroup with memory.pressure found for container {container_id!r}. "
        f"Tried: {', '.join(str(c) for c in candidates)}. On cgroup v1 hosts "
        "per-container PSI is not available at all."
    )
