"""Single seam between the fork and upstream fxhoudinimcp / the MCP SDK.

Every upstream import and every private SDK attribute the fork relies on is
listed here, so an upstream rename or SDK bump breaks exactly one file. The
rest of ``fxhoudinimcp._fork`` imports only from this module.

Upstream symbols used (if one disappears, find its replacement upstream and
re-point the import below; do not edit upstream files):

* ``fxhoudinimcp._sdk.Context``                  -- tool context type
* ``fxhoudinimcp._sdk.forbid_unknown_arguments`` -- strict argument models
* ``fxhoudinimcp._sdk`` module attribute ``run_server`` -- wrapped by the CLI
  hook so progressive mode is applied right before the server starts

Private SDK attributes used (identical on mcp 1.x and 2.x today):

* ``server._tool_manager`` and its ``_tools`` dict / ``list_tools()``
* ``server._lowlevel_server.instructions`` (2.x) or
  ``server._mcp_server.instructions`` (1.x)
"""

from __future__ import annotations

# Built-in
from typing import Any

# Internal
from fxhoudinimcp import _sdk as sdk_module
from fxhoudinimcp._sdk import Context, forbid_unknown_arguments

try:
    # mcp >= 2.0
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - depends on the installed mcp
    # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError

try:
    # mcp >= 2.x wraps unexpected tool exceptions in this ToolError subclass.
    from mcp.server.mcpserver.exceptions import UnexpectedToolError
except ImportError:  # pragma: no cover - depends on the installed mcp
    UnexpectedToolError = None  # type: ignore[assignment, misc]


def tool_registry(server: Any) -> dict[str, Any]:
    """The server's live ``{name: Tool}`` mapping (private on both SDK majors)."""
    return server._tool_manager._tools


def list_tool_objects(server: Any) -> list[Any]:
    """All SDK ``Tool`` objects currently registered on *server*, in order."""
    return list(server._tool_manager.list_tools())


def new_tool_manager(server: Any, tools: list[Any]) -> Any:
    """A fresh ToolManager of the server's own class, holding *tools*."""
    manager = type(server._tool_manager)(warn_on_duplicate_tools=False)
    for tool in tools:
        manager._tools[tool.name] = tool
    return manager


def get_instructions(server: Any) -> str:
    """The server's advertised instructions, on either SDK major."""
    for attr in ("_lowlevel_server", "_mcp_server"):
        inner = getattr(server, attr, None)
        if inner is not None:
            return inner.instructions or ""
    return getattr(server, "instructions", None) or ""


def set_instructions(server: Any, text: str) -> None:
    """Replace the server's advertised instructions, on either SDK major."""
    for attr in ("_lowlevel_server", "_mcp_server"):
        inner = getattr(server, attr, None)
        if inner is not None:
            inner.instructions = text
            return
    raise RuntimeError("Cannot locate the low-level MCP server to set instructions on.")


def tool_category(tool: Any) -> str:
    """Category of an upstream tool: the name of the module that defines it.

    Upstream keeps one module per category under ``fxhoudinimcp/tools/`` and
    its compact-JSON wrapper keeps ``__module__`` via ``functools.wraps``.
    """
    module = getattr(getattr(tool, "fn", None), "__module__", "") or ""
    return module.rsplit(".", 1)[-1] or "misc"


__all__ = [
    "Context",
    "ToolError",
    "UnexpectedToolError",
    "forbid_unknown_arguments",
    "get_instructions",
    "list_tool_objects",
    "new_tool_manager",
    "sdk_module",
    "set_instructions",
    "tool_category",
    "tool_registry",
]
