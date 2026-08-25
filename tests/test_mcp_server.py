"""Tests for the MCP binding.

Skipped wholesale when the SDK is absent. The tool layer itself is covered by
``test_mcp_tools.py``, which has no such dependency — the split is the point:
everything worth testing about *what the tools measure* stays testable on a
machine with neither the SDK nor a PSI kernel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="needs the mcp extra: pip install 'syshealth[mcp]'")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from syshealth.mcp import ReplaySource, build_tools, load_run  # noqa: E402
from syshealth.mcp.server import (  # noqa: E402
    _surface_input_errors,
    build_server,
    instructions_for,
)
from syshealth.mcp.tools import ToolInputError  # noqa: E402

RUNS = Path(__file__).parent / "fixtures" / "runs"


@pytest.fixture
def source() -> ReplaySource:
    return ReplaySource(load_run(RUNS / "cache-heavy.jsonl"), label="cache-heavy")


@pytest.fixture
def local_tools(source: ReplaySource) -> dict:
    return build_tools(source)


def registered(tools: dict) -> dict:
    """``MCPServer.list_tools`` is a coroutine; the suite has no async plugin."""
    return {t.name: t for t in asyncio.run(build_server(tools).list_tools())}


def test_every_tool_is_registered_and_advertised_read_only(local_tools: dict):
    tools = registered(local_tools)

    assert set(tools) == {
        "get_health",
        "get_memory_pressure",
        "get_cpu_pressure",
        "get_reclaim_activity",
    }
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is True, name
        assert tool.annotations.destructive_hint is False, name


def test_the_wrapper_does_not_hide_the_tools_schema(local_tools: dict):
    """``functools.wraps`` is what keeps the derived schema intact.

    Without ``__wrapped__`` the SDK would see ``(*args, **kwargs)`` and
    advertise a tool taking anything at all.
    """
    for tool in registered(local_tools).values():
        properties = tool.input_schema.get("properties", {})
        assert "window_s" in properties, tool.name
        assert properties["window_s"]["type"] == "number", tool.name


def test_instructions_describe_only_the_tools_that_are_registered(local_tools: dict):
    """A fleet-only server must not tell the model it can measure "this
    machine". The names are checked explicitly because get_fleet_summary would
    satisfy any get_*-shaped heuristic and quietly reintroduce that claim."""
    from syshealth.mcp import build_fleet_tools
    from syshealth.store import Store

    fleet_tools = build_fleet_tools(Store(":memory:"))

    local_only = instructions_for(local_tools)
    fleet_only = instructions_for(fleet_tools)
    both = instructions_for({**local_tools, **fleet_tools})

    assert "THIS machine" in local_only and "across the\nfleet" not in local_only
    assert "THIS machine" not in fleet_only and "fleet" in fleet_only
    assert "THIS machine" in both and "list_nodes" in both
    # The saturation-vs-utilisation framing is not optional in any of them.
    assert all("Saturation is not utilisation" in text for text in (local_only, fleet_only, both))


def test_an_anticipated_rejection_keeps_its_message():
    """The bounds text is the only way a caller learns how to retry."""

    def handler(window_s: float = 2.0) -> dict:
        raise ToolInputError("window_s must be between 0.2 and 30.0 seconds, got 999.0")

    with pytest.raises(ToolError, match="between 0.2 and 30.0"):
        _surface_input_errors(handler)()


def test_an_unexpected_crash_is_not_dressed_up_as_a_tool_error():
    """Only anticipated rejections get their text returned. Everything else
    stays a crash, so the SDK withholds internals from the client."""

    def handler(window_s: float = 2.0) -> dict:
        raise RuntimeError("/proc/pressure/memory: permission denied for uid 1000")

    with pytest.raises(RuntimeError):
        _surface_input_errors(handler)()


def test_a_valid_call_passes_straight_through(local_tools: dict):
    handler = _surface_input_errors(local_tools["get_health"].handler)
    result = handler(window_s=2.0)

    assert result["source"] == "replay:cache-heavy"
    assert result["state"] in {"HEALTHY", "DEGRADED", "SATURATED", "UNKNOWN"}
