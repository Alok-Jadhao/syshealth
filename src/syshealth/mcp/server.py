"""The MCP binding. The only module in SysHealth that imports the SDK.

Kept deliberately thin: it maps ``Tool`` objects onto the SDK's registration
call and starts a transport. Everything worth testing lives in ``tools.py``,
which has no SDK dependency and therefore no reason to be untested on a
machine where the SDK is not installed.

One transport-level rule matters more than it looks: on stdio, **stdout is
the protocol channel**. Anything printed there that is not a JSON-RPC frame
corrupts the session. Diagnostics go to stderr, always.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .. import __version__
from .tools import Tool, ToolInputError

BASE_INSTRUCTIONS = """\
SysHealth measures whether a Linux machine is *saturated* — the share of
wall-clock time its tasks spent unable to make progress — using kernel
Pressure Stall Information.

Saturation is not utilisation. Utilisation is how full a resource is;
saturation is how much work was lost waiting for it. Linux fills otherwise
idle RAM with page cache, so a healthy server routinely reports 90%+ memory
"used" while stalling zero, and a machine reporting a comfortable 60% can be
in continuous reclaim. Diagnose from the stall percentages. Treat utilisation
as context only.

If `psi_available` is false, nothing was measured and the state is UNKNOWN —
that is not the same as healthy.

All tools here are read-only. Nothing in this server can change the state of
any machine.
"""

LOCAL_INSTRUCTIONS = """\
The get_* tools taking `window_s` measure THIS machine over one fresh window,
and report how long they measured for and where the data came from. A `source`
beginning with `replay:` is a recording, not a live machine. One short window
is a weak signal: a calm window does not exonerate a thrashing box, and a
single spike does not condemn a healthy one.
"""

FLEET_INSTRUCTIONS = """\
The node and fleet tools read stored telemetry pushed by agents across the
fleet. They fold a node's whole history into percentiles, which is much
stronger evidence than any single window, so prefer get_node_health over a
live reading when a node name is available. Start from list_nodes or
get_fleet_summary to find which node is worth investigating.
"""


# Which set is present is decided by naming one tool from each, not by
# pattern-matching the names: "get_fleet_summary" starts with get_ and has no
# "node" in it, so any such heuristic would advertise local instructions to a
# fleet-only server and mislead the model about what it is looking at.
LOCAL_MARKER = "get_health"
FLEET_MARKER = "list_nodes"


def instructions_for(tools: dict[str, Tool]) -> str:
    """Describe only the tools this process actually registered."""
    parts = [BASE_INSTRUCTIONS]
    if LOCAL_MARKER in tools:
        parts.append(LOCAL_INSTRUCTIONS)
    if FLEET_MARKER in tools:
        parts.append(FLEET_INSTRUCTIONS)
    return "\n".join(parts)


def _surface_input_errors(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Let a rejected argument explain itself to the caller.

    The SDK deliberately withholds the text of an unexpected exception,
    returning only "Error executing tool <name>". That is the right default —
    a crash should not leak internals — but it also swallows the bounds
    message a model needs in order to retry correctly. ``ToolError`` is the
    SDK's channel for a failure the tool anticipated, so validation errors go
    through it and everything else keeps the generic treatment.

    ``functools.wraps`` sets ``__wrapped__``, which is what lets the SDK still
    derive this tool's schema from the real handler's signature.
    """

    @functools.wraps(handler)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return handler(*args, **kwargs)
        except ToolInputError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(tools: dict[str, Tool]) -> MCPServer:
    """Register a composed set of tools. The caller decides which sets."""
    server = MCPServer(
        name="syshealth",
        version=__version__,
        instructions=instructions_for(tools),
    )

    for tool in tools.values():
        server.add_tool(
            _surface_input_errors(tool.handler),
            name=tool.name,
            description=tool.description,
            # The tier is the source of truth; the protocol hints are a
            # projection of it, so the two cannot drift apart.
            annotations=ToolAnnotations(
                read_only_hint=tool.tier.read_only,
                destructive_hint=tool.tier.destructive,
                idempotent_hint=tool.tier.read_only,
            ),
        )

    return server


def run_server(tools: dict[str, Tool], notes: list[str] | None = None) -> int:
    tiers = sorted({tool.tier.value for tool in tools.values()})
    print(
        f"syshealth mcp: {len(tools)} tools ({', '.join(tiers)}) — "
        f"{', '.join(sorted(tools))}",
        file=sys.stderr,
    )
    for note in notes or []:
        print(note, file=sys.stderr)

    build_server(tools).run(transport="stdio")
    return 0
