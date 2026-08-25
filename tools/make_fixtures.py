#!/usr/bin/env python3
"""Generate the recorded-run fixtures in ``tests/fixtures/runs``.

These exist so that the analysis, verdict and reporting paths can be exercised
on any machine — including CI runners and macOS laptops, which have no PSI.
They are generated rather than hand-written so that the shape of each scenario
is stated in code and can be re-derived if the Interval schema changes.

Run with::

    python tools/make_fixtures.py

The three scenarios are deliberately chosen to be the three cases that matter:

``thrashing``    memory genuinely saturated — the tool must say UNDERSIZED
``cache-heavy``  utilisation looks terrifying, saturation is zero — the false
                 positive that drives most over-provisioning, and the finding
                 this project exists to demonstrate
``idle-oversized`` a big box doing very little — the money case
"""

from __future__ import annotations

import json
import math
import pathlib
import random
import zlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "runs"
GB_KB = 1024 * 1024


def sample(
    t: float,
    duration: float,
    mem_some: float,
    mem_full: float,
    cpu_some: float,
    io_some: float,
    mem_used_pct: float,
    mem_naive_used_pct: float,
    mem_total_kb: int,
    cpu_busy: float,
    pgscan_direct: int = 0,
    pgmajfault: int = 0,
    pswpin: int = 0,
    oom_kill: int = 0,
) -> dict:
    return {
        "start_wall": t,
        "end_wall": t + duration,
        "duration_s": duration,
        "stall": {
            "memory.some": round(max(mem_some, 0.0), 4),
            "memory.full": round(max(mem_full, 0.0), 4),
            "cpu.some": round(max(cpu_some, 0.0), 4),
            "io.some": round(max(io_some, 0.0), 4),
            "io.full": 0.0,
        },
        "avg10": {},
        "mem_used_pct": round(mem_used_pct, 2),
        "mem_naive_used_pct": round(mem_naive_used_pct, 2),
        "mem_total_kb": mem_total_kb,
        "swap_used_pct": 0.0,
        "cpu_busy_pct": round(cpu_busy, 2),
        "reclaim": {
            "pgscan_direct": pgscan_direct,
            "pgsteal_direct": int(pgscan_direct * 0.8),
            "pgscan_kswapd": pgscan_direct * 2,
            "pgsteal_kswapd": int(pgscan_direct * 1.6),
            "pgmajfault": pgmajfault,
            "pswpin": pswpin,
            "pswpout": pswpin,
            "oom_kill": oom_kill,
        },
        "psi_available": True,
    }


def thrashing(rng: random.Random) -> list[dict]:
    """A 1 GB box running a workload that needs roughly 1.6 GB.

    Modelled on the real result already in the repo: the t3.micro under
    ``stress --vm 2 --vm-bytes 800M``.
    """
    out = []
    t, step, n = 1_700_000_000.0, 2.0, 90
    for i in range(n):
        # Pressure ramps as the working set is faulted in, then plateaus high.
        ramp = min(i / 12.0, 1.0)
        base = 14.0 * ramp
        out.append(
            sample(
                t=t + i * step,
                duration=step,
                mem_some=base + rng.uniform(-1.5, 3.0),
                mem_full=base * 0.35 + rng.uniform(-0.4, 1.0),
                cpu_some=rng.uniform(0.2, 1.4),
                io_some=base * 0.25 + rng.uniform(0, 1.2),
                mem_used_pct=72 + 22 * ramp + rng.uniform(-1, 1),
                mem_naive_used_pct=88 + 10 * ramp + rng.uniform(-1, 1),
                mem_total_kb=int(0.95 * GB_KB),
                cpu_busy=55 + rng.uniform(-8, 20),
                pgscan_direct=int(1800 * ramp) + rng.randint(0, 400),
                pgmajfault=int(220 * ramp) + rng.randint(0, 60),
                pswpin=int(90 * ramp) + rng.randint(0, 40),
            )
        )
    return out


def cache_heavy(rng: random.Random) -> list[dict]:
    """An 8 GB box whose page cache is full. The classic false alarm.

    Utilisation reads 90%+ and every conventional dashboard turns amber. PSI
    reports essentially nothing, because reclaimable cache is not pressure.
    """
    out = []
    t, step, n = 1_700_000_000.0, 2.0, 200
    for i in range(n):
        wobble = math.sin(i / 9.0)
        out.append(
            sample(
                t=t + i * step,
                duration=step,
                mem_some=max(0.0, rng.uniform(0.0, 0.09)),
                mem_full=0.0,
                cpu_some=max(0.0, 0.4 + wobble * 0.3 + rng.uniform(0, 0.5)),
                io_some=max(0.0, 0.8 + wobble * 0.6 + rng.uniform(0, 0.9)),
                # The whole point of this scenario: the used/free rule reads
                # ~93% because page cache is counted as used, while the actual
                # working set is barely a quarter of RAM and nothing stalls.
                mem_used_pct=27 + rng.uniform(-2, 3),
                mem_naive_used_pct=92 + rng.uniform(-1.5, 2.5),
                mem_total_kb=int(7.7 * GB_KB),
                cpu_busy=38 + wobble * 12 + rng.uniform(-5, 6),
                pgscan_direct=0,
                pgmajfault=rng.randint(0, 3),
            )
        )
    return out


def idle_oversized(rng: random.Random) -> list[dict]:
    """A t3.large running a service that needs well under 2 GB."""
    out = []
    t, step, n = 1_700_000_000.0, 2.0, 400
    for i in range(n):
        out.append(
            sample(
                t=t + i * step,
                duration=step,
                mem_some=0.0 if rng.random() > 0.05 else rng.uniform(0, 0.04),
                mem_full=0.0,
                cpu_some=rng.uniform(0.0, 0.25),
                io_some=rng.uniform(0.0, 0.4),
                mem_used_pct=14.5 + rng.uniform(-1.2, 1.8),
                mem_naive_used_pct=31 + rng.uniform(-3, 4),
                mem_total_kb=int(7.7 * GB_KB),
                cpu_busy=9 + rng.uniform(-3, 7),
            )
        )
    return out


SCENARIOS = {
    "thrashing": thrashing,
    "cache-heavy": cache_heavy,
    "idle-oversized": idle_oversized,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in SCENARIOS.items():
        # Fixed seed: fixtures must be byte-identical run to run, or every
        # regeneration shows up as noise in the diff. Note this is crc32 and
        # not hash(): Python randomises string hashing per process unless
        # PYTHONHASHSEED is pinned, which would silently defeat the point.
        rng = random.Random(zlib.crc32(name.encode()))
        rows = build(rng)
        path = OUT / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
        print(f"{path.relative_to(OUT.parent.parent.parent)}  {len(rows)} samples")


if __name__ == "__main__":
    main()
