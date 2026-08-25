#!/usr/bin/env python3
"""Drive the SysHealth MCP server as a real client, over a real stdio pipe.

The unit tests call the tool handlers directly, which proves the measurements
are right but not that the server is reachable. This proves the other half:
that a client can spawn it, complete the protocol handshake, discover the
tools with their schemas and permission hints, invoke them, and get a bad
argument refused with an explanation.

Both modes are covered. The local tools replay a recorded run and the fleet
tools read a database built from the same recordings, so neither needs a PSI
kernel, a loaded machine, or a running server — which is what lets this run on
the macOS CI leg too.

    python tools/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syshealth.mcp import load_run  # noqa: E402
from syshealth.store import Store  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "tests" / "fixtures" / "runs"

LOCAL_TOOLS = {"get_health", "get_memory_pressure", "get_cpu_pressure", "get_reclaim_activity"}
FLEET_TOOLS = {"list_nodes", "get_node_health", "get_node_verdict", "get_fleet_summary"}

# node -> (recorded run, the instance type it was recorded on)
FLEET = {
    "web-01": ("thrashing", "t3.micro"),
    "cache-01": ("cache-heavy", "t3.large"),
    "batch-01": ("idle-oversized", "t3.2xlarge"),
}

failures = 0


def check(condition: bool, label: str) -> None:
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    failures += not condition


def build_fleet_db(path: Path) -> None:
    store = Store(path)
    now = time.time()
    for node, (run, instance_type) in FLEET.items():
        samples = load_run(RUNS / f"{run}.jsonl")
        for index, sample in enumerate(samples):
            store.record(
                node=node,
                payload=sample.to_dict(),
                instance_type=instance_type,
                now=now - (len(samples) - index) * 2.0,
            )
    store.close()


async def session_for(args: list[str]):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "syshealth", "mcp", *args],
        cwd=str(REPO),
    )


async def check_local() -> None:
    print("\nlocal tools (replaying a recorded run)")
    params = await session_for(["--replay", str(RUNS / "cache-heavy.jsonl")])

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        check(set(tools) == LOCAL_TOOLS, f"advertises {len(LOCAL_TOOLS)} local tools")
        check(
            all(t.annotations and t.annotations.read_only_hint for t in tools.values()),
            "every tool is hinted read-only",
        )
        check(
            all("window_s" in t.input_schema.get("properties", {}) for t in tools.values()),
            "every tool advertises its window_s parameter",
        )

        for name in sorted(LOCAL_TOOLS):
            result = await session.call_tool(name, {"window_s": 2.0})
            check(not result.is_error, f"{name} returned a result")
            payload = json.loads(result.content[0].text)
            check(payload["source"].startswith("replay:"), f"{name} names its source")

        refused = await session.call_tool("get_health", {"window_s": 999})
        check(refused.is_error, "an out-of-range window is refused")
        check(
            "between 0.2 and 30.0" in refused.content[0].text,
            "the refusal explains the bound rather than saying only 'error'",
        )


async def check_fleet(db: Path) -> None:
    print("\nfleet tools (reading stored telemetry)")
    params = await session_for(["--db", str(db), "--fleet-only"])

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        check(set(tools) == FLEET_TOOLS, f"advertises {len(FLEET_TOOLS)} fleet tools")
        check(
            all(t.annotations and t.annotations.read_only_hint for t in tools.values()),
            "every tool is hinted read-only",
        )

        nodes = json.loads((await session.call_tool("list_nodes", {})).content[0].text)
        check(nodes["count"] == len(FLEET), f"list_nodes finds all {len(FLEET)} nodes")

        health = json.loads(
            (await session.call_tool("get_node_health", {"node": "web-01"})).content[0].text
        )
        check(health["state"] == "SATURATED", "the thrashing node reads SATURATED over history")
        check(health["bottleneck"] == "memory", "its bottleneck is memory")

        verdict = json.loads(
            (await session.call_tool("get_node_verdict", {"node": "web-01"})).content[0].text
        )
        check(verdict["sizing"] == "UNDERSIZED", "it is told to grow")
        check(
            bool(verdict["evidence"]) and bool(verdict["reasons"]),
            "the verdict carries the evidence it was reached from",
        )

        fleet = json.loads((await session.call_tool("get_fleet_summary", {})).content[0].text)
        check(fleet["by_state"]["SATURATED"] == 1, "the fleet roll-up counts one saturated node")
        check(
            fleet["needs_attention"][0]["node"] == "web-01",
            "the worst node is ranked first",
        )

        refused = await session.call_tool("get_node_health", {"node": "nope-01"})
        check(refused.is_error, "an unknown node is refused")
        check(
            "web-01" in refused.content[0].text,
            "the refusal lists the nodes that do exist",
        )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fleet.db"
        build_fleet_db(db)
        await check_local()
        await check_fleet(db)

    if failures:
        raise SystemExit(f"\n{failures} check(s) failed")
    print("\nall checks passed")


if __name__ == "__main__":
    asyncio.run(main())
