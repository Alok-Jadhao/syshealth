"""The MCP tool layer.

SysHealth's measurement stack is a pipeline of plain functions over plain
data: ``ProcReader`` -> ``Snapshot`` -> ``Interval`` -> ``summarise`` ->
``evaluate``. This subpackage exposes a slice of that pipeline as Model
Context Protocol tools, so an agent can *measure* a machine rather than be
told about it in prose.

Three rules shape the layout, and breaking any of them costs more later than
it saves now:

1. ``tools.py`` never imports the MCP SDK. Tools are plain callables that
   return JSON-able dicts, so they can be tested with no SDK installed (CI
   runs on macOS on purpose) and called in-process by a future agent without
   a socket in the middle.
2. ``server.py`` is the only module that touches the SDK, and the SDK is an
   optional extra. The core package still has zero runtime dependencies.
3. Every tool carries a permission tier from the day it is written. Phase 1
   is entirely read-only, but a policy layer retrofitted onto tools that were
   born without one is how these systems end up unsafe.

Note the name: this package is ``syshealth.mcp`` and the SDK is the top-level
``mcp``. Python 3 has no implicit relative imports, so ``import mcp`` inside
this package resolves to the SDK, as intended.
"""

from __future__ import annotations

from .sources import LiveSource, MetricSource, ReplaySource, load_run
from .tools import Tier, Tool, build_tools

__all__ = [
    "LiveSource",
    "MetricSource",
    "ReplaySource",
    "Tier",
    "Tool",
    "build_tools",
    "load_run",
]
