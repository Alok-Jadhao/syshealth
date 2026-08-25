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
from .sources import MetricSource
from .tools import ToolInputError, build_tools

INSTRUCTIONS = """\
SysHealth measures whether a Linux machine is *saturated* — the share of
wall-clock time its tasks spent unable to make progress — using kernel
Pressure Stall Information.

Saturation is not utilisation. Utilisation is how full a resource is;
saturation is how much work was lost waiting for it. Linux fills otherwise
idle RAM with page cache, so a healthy server routinely reports 90%+ memory
"used" while stalling zero, and a machine reporting a comfortable 60% can be
in continuous reclaim. Diagnose from the stall percentages. Treat utilisation
as context only.

Every tool measures a fresh window and reports how long it measured for and
where the data came from. A `source` beginning with `replay:` is a recording,
not a live machine. If `psi_available` is false, nothing was measured and the
state is UNKNOWN — that is not the same as healthy.

All tools here are read-only. Nothing in this server can change the state of
the machine.
"""


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


def build_server(source: MetricSource) -> MCPServer:
    """Register every tool against one measurement source."""
    server = MCPServer(
        name="syshealth",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    for tool in build_tools(source).values():
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


def run_server(source: MetricSource) -> int:
    tools = build_tools(source)
    print(
        f"syshealth mcp: {len(tools)} read-only tools over {source.name}",
        file=sys.stderr,
    )
    if not getattr(source, "psi_available", True):
        print(
            "warning: no PSI on this kernel — every tool will report "
            "psi_available=false and state UNKNOWN. Use --replay to serve a "
            "recorded run instead.",
            file=sys.stderr,
        )

    build_server(source).run(transport="stdio")
    return 0
