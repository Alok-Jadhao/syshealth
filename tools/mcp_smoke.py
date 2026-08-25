#!/usr/bin/env python3
"""Drive the SysHealth MCP server as a real client, over a real stdio pipe.

The unit tests call the tool handlers directly, which proves the measurements
are right but not that the server is reachable. This proves the other half:
that a client can spawn it, complete the protocol handshake, discover the
tools with their schemas and permission hints, invoke them, and get a bad
argument refused with an explanation.

It replays a recorded run, so it needs neither a PSI kernel nor a loaded
machine, and therefore runs on the macOS CI leg too.

    python tools/mcp_smoke.py [run.jsonl]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RUN = REPO / "tests" / "fixtures" / "runs" / "cache-heavy.jsonl"

EXPECTED = {"get_health", "get_memory_pressure", "get_cpu_pressure", "get_reclaim_activity"}


def check(condition: bool, label: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(f"smoke test failed: {label}")


async def main(run: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "syshealth", "mcp", "--replay", str(run)],
        cwd=str(REPO),
    )

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        print(f"connected to {init.server_info.name} {init.server_info.version}")

        print("\ndiscovery")
        tools = {t.name: t for t in (await session.list_tools()).tools}
        check(set(tools) == EXPECTED, f"advertises {len(EXPECTED)} tools")
        check(
            all(t.annotations and t.annotations.read_only_hint for t in tools.values()),
            "every tool is hinted read-only",
        )
        check(
            all("window_s" in t.input_schema.get("properties", {}) for t in tools.values()),
            "every tool advertises its window_s parameter",
        )

        print("\ninvocation")
        for name in sorted(EXPECTED):
            result = await session.call_tool(name, {"window_s": 2.0})
            check(not result.is_error, f"{name} returned a result")
            payload = json.loads(result.content[0].text)
            check(payload["source"].startswith("replay:"), f"{name} names its source")
            check(payload["measured_s"] > 0, f"{name} reports its measured window")

        print("\nargument validation")
        refused = await session.call_tool("get_health", {"window_s": 999})
        check(refused.is_error, "an out-of-range window is refused")
        check(
            "between 0.2 and 30.0" in refused.content[0].text,
            "the refusal explains the bound rather than saying only 'error'",
        )

    print("\nall checks passed")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RUN))
