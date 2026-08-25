#!/usr/bin/env python3
"""Build a small, reproducible fleet database for demos.

Three nodes, each replaying one of the project's own recorded scenarios, so
the numbers a demo shows are real measurements from `tools/make_fixtures.py`
rather than anything invented for the occasion:

    web-01     thrashing        genuine memory saturation
    cache-01   cache-heavy      the false alarm: 90%+ "used", zero stalling
    batch-01   idle-oversized   a large box doing little, the money case

The fixtures are fixed-seed and reproducible (see the README), so re-running
this script produces the same fleet every time.

    python tools/demo_setup.py [output-dir]   # default: demo/
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from syshealth.mcp import load_run  # noqa: E402
from syshealth.store import Store  # noqa: E402

RUNS = REPO / "tests" / "fixtures" / "runs"

FLEET = {
    "web-01": ("thrashing", "t3.micro"),
    "cache-01": ("cache-heavy", "t3.large"),
    "batch-01": ("idle-oversized", "t3.2xlarge"),
}


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "demo"
    out.mkdir(parents=True, exist_ok=True)

    fleet_db = out / "fleet.db"
    incidents_db = out / "incidents.db"
    for stale in (fleet_db, incidents_db):
        stale.unlink(missing_ok=True)
        Path(f"{stale}-wal").unlink(missing_ok=True)
        Path(f"{stale}-shm").unlink(missing_ok=True)

    store = Store(fleet_db)
    now = time.time()
    for node, (run, instance_type) in FLEET.items():
        samples = load_run(RUNS / f"{run}.jsonl")
        for index, sample in enumerate(samples):
            store.record(
                node=node,
                payload=sample.to_dict(),
                instance_type=instance_type,
                address="10.0.0.1",
                now=now - (len(samples) - index) * 2.0,
            )
        print(f"  {node:<10} <- {run:<15} ({len(samples)} samples, {instance_type})")

    print(f"\n{store.count()} samples across {len(FLEET)} nodes -> {fleet_db}")
    print(f"incidents will be recorded to -> {incidents_db}")


if __name__ == "__main__":
    main()
