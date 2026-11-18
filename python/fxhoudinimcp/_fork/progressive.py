"""Progressive disclosure: the fork's server exposes four gateway tools, always.

This fork is progressive-only. The server advertises just ``houdini_discover``,
``houdini_schema``, ``houdini_call`` and ``houdini_batch``. Every upstream tool
stays registered in a private :class:`~fxhoudinimcp._fork.catalog.ToolCatalog`
and runs through the exact SDK ``Tool`` object upstream registered. Anyone who
wants the full 200+ tool surface installs upstream fxhoudinimcp instead.

How it attaches to upstream, without editing upstream code beyond one hook:

1. ``fxhoudinimcp.__main__.main`` starts with lines marked
   ``# fork:progressive`` that call ``fxhoudinimcp._fork.install_hook``, which
   calls :func:`install`.
2. :func:`install` strips the legacy ``--exposure progressive`` flag from
   ``sys.argv`` (so old client configs keep working and upstream's argument
   handling never sees it), and wraps ``fxhoudinimcp._sdk.run_server``.
3. When upstream calls ``run_server(mcp, ...)`` -- after every tool, prompt and
   resource is registered -- the wrapper calls :func:`apply_progressive`, then
   starts the server unchanged.
"""

from __future__ import annotations

# Built-in
import functools
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Internal
from fxhoudinimcp._fork.catalog import BatchCallItem, ToolCatalog
from fxhoudinimcp._fork.upstream_api import (
    Context,
    forbid_unknown_arguments,
    get_instructions,
    list_tool_objects,
    sdk_module,
    set_instructions,
    tool_registry,
)

LEGACY_EXPOSURE_ENV = "FXHOUDINIMCP_EXPOSURE"
GATEWAY_TOOL_NAMES = ("houdini_discover", "houdini_schema", "houdini_call", "houdini_batch")
HOOK_MARKER = "# fork:progressive"

_PREAMBLE_PATH = Path(__file__).with_name("preamble.md")
_STATE_ATTR = "_fork_progressive_state"
_WRAPPED_FLAG = "_fork_progressive_wrapped"
_FULL_HINT = (
    "This fork only serves the progressive gateway; for the full tool set "
    "install upstream instead: pip install fxhoudinimcp"
)


# -- legacy exposure flag ------------------------------------------------------


def check_legacy_mode(raw: str | None, *, source: str) -> None:
    """Accept an old ``progressive`` / empty setting; refuse anything else."""
    value = (raw or "").strip().lower()
    if value in ("", "progressive"):
        return
    if value == "full":
        raise ValueError(f"{source}=full is not supported. {_FULL_HINT}")
    raise ValueError(f"Invalid {source} {raw!r}; this fork is progressive-only.")


def consume_cli_args(args: list[str]) -> list[str]:
    """*args* without the legacy ``--exposure progressive`` flag.

    Raises ``ValueError`` for a missing value or any mode but ``progressive``.
    """
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--exposure":
            value = args[index + 1] if index + 1 < len(args) else ""
            index += 2
        elif arg.startswith("--exposure="):
            value = arg.split("=", 1)[1]
            index += 1
        else:
            remaining.append(arg)
            index += 1
            continue
        if not value.strip() or value.startswith("-"):
            raise ValueError("missing value for --exposure (only 'progressive' is accepted)")
        check_legacy_mode(value, source="--exposure")
    return remaining


# -- CLI hook --------------------------------------------------------------


def install(argv: list[str] | None = None) -> None:
    """Entry-point hook, called first thing in upstream ``main()``.

    Mutates *argv* (default ``sys.argv``) in place to drop the legacy
    ``--exposure`` flag and makes ``fxhoudinimcp._sdk.run_server`` expose the
    gateway. Exits 2 on a request for any other mode.
    """
    argv = sys.argv if argv is None else argv
    try:
        remaining = consume_cli_args(list(argv[1:]))
    except ValueError as exc:
        print(f"{exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc
    argv[1:] = remaining
    _wrap_run_server()


def _wrap_run_server() -> None:
    original = sdk_module.run_server
    if getattr(original, _WRAPPED_FLAG, False):
        return

    @functools.wraps(original)
    def run_server(server, *args, **kwargs):
        try:
            check_legacy_mode(os.getenv(LEGACY_EXPOSURE_ENV), source=LEGACY_EXPOSURE_ENV)
        except ValueError as exc:
            print(f"{exc}\n", file=sys.stderr)
            raise SystemExit(2) from exc
        apply_progressive(server)
        try:
            return original(server, *args, **kwargs)
        finally:
            # run_server blocks for the life of the process, so this only
            # matters when it returns early (tests patch server.run): the
            # shared module-level server must not stay mutated afterwards.
            restore_full(server)

    setattr(run_server, _WRAPPED_FLAG, True)
    sdk_module.run_server = run_server


# -- applying / reverting ----------------------------------------------------


@dataclass
class _State:
    catalog: ToolCatalog
    tools: list[Any]
    instructions: str


def current_catalog(server: Any) -> ToolCatalog | None:
    """The catalog behind *server*'s gateway, or ``None`` before it is applied."""
    state = getattr(server, _STATE_ATTR, None)
    return state.catalog if state is not None else None


def apply_progressive(server: Any) -> ToolCatalog:
    """Move every registered tool into a catalog and expose the 4 gateways.

    Idempotent. Call it only after all tool modules are imported.
    """
    state: _State | None = getattr(server, _STATE_ATTR, None)
    if state is not None:
        return state.catalog

    tools = list_tool_objects(server)
    if not tools:
        raise RuntimeError(
            "No tools are registered; import fxhoudinimcp.tools before applying progressive mode."
        )
    clash = sorted(set(GATEWAY_TOOL_NAMES) & {tool.name for tool in tools})
    if clash:
        raise RuntimeError(f"Upstream now registers tools named like the gateway: {clash}.")

    original_instructions = get_instructions(server)
    catalog = ToolCatalog(server, tools)

    registry = tool_registry(server)
    registry.clear()
    register_gateway_tools(server, catalog)
    set_instructions(server, build_instructions(catalog, original_instructions))

    setattr(server, _STATE_ATTR, _State(catalog, tools, original_instructions))
    return catalog


def restore_full(server: Any) -> None:
    """Undo :func:`apply_progressive` (used by tests)."""
    state: _State | None = getattr(server, _STATE_ATTR, None)
    if state is None:
        return
    registry = tool_registry(server)
    registry.clear()
    for tool in state.tools:
        registry[tool.name] = tool
    set_instructions(server, state.instructions)
    delattr(server, _STATE_ATTR)


def build_instructions(catalog: ToolCatalog, upstream_instructions: str) -> str:
    """Gateway preamble, then upstream's own instructions verbatim.

    Reusing upstream's text means its rules never drift from the fork's.
    """
    categories = catalog.categories()
    preamble = _PREAMBLE_PATH.read_text(encoding="utf-8").format(
        tool_count=len(catalog),
        category_count=len(categories),
        categories=", ".join(categories),
    )
    upstream = upstream_instructions.strip()
    return f"{preamble.rstrip()}\n\n---\n\n{upstream}\n" if upstream else preamble


# -- gateway tools -----------------------------------------------------------


def _dumps(result: Any) -> Any:
    """Compact JSON for dict results; tool payloads are already compact strings."""
    if isinstance(result, dict):
        return json.dumps(result, separators=(",", ":"), ensure_ascii=False, default=str)
    return result


def register_gateway_tools(server: Any, catalog: ToolCatalog) -> None:
    """Register the four gateway tools on *server* with strict argument models."""

    async def houdini_discover(
        query: str | None = None,
        category: str | None = None,
        limit: int = 30,
    ) -> dict:
        """Browse categories or search all Houdini MCP tools without loading schemas.

        Call with no arguments to list every category and its tool names.
        Pass ``category`` and/or space-separated ``query`` keywords to view
        tool summaries and required/optional argument names.

        Args:
            query: Optional space-separated keywords matched against tool names,
                categories, parameter names, and descriptions.
            category: Optional category name (e.g. "nodes", "lops", "geometry").
            limit: Maximum number of matching tools to return (1-250).
        """
        return _dumps(catalog.discover(query=query, category=category, limit=limit))

    async def houdini_schema(
        tool_name: str | None = None,
        tool_names: list[str] | None = None,
    ) -> dict:
        """Get full docstrings and parameter JSON schemas for Houdini tools.

        Pass ``tool_name`` for a single tool, or ``tool_names`` to inspect
        multiple tools in one round-trip before calling ``houdini_call`` or
        ``houdini_batch``.

        Args:
            tool_name: Single Houdini tool name.
            tool_names: List of Houdini tool names to inspect together.
        """
        return _dumps(catalog.schema_for(tool_name=tool_name, tool_names=tool_names))

    async def houdini_call(
        ctx: Context,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Validate and execute any registered Houdini tool by name.

        Enforces the target tool's strict parameter schema (including unknown
        argument rejection with did-you-mean hints). If validation fails, the
        error automatically includes the target tool's docstring and schema.

        Args:
            tool_name: Target Houdini tool name (from ``houdini_discover``).
            arguments: Keyword arguments object for the target tool.
        """
        return _dumps(await catalog.call(tool_name, arguments, ctx))

    async def houdini_batch(
        ctx: Context,
        calls: list[BatchCallItem],
        stop_on_error: bool = True,
    ) -> dict:
        """Execute multiple Houdini tool calls sequentially in one round-trip.

        Each item is ``{"tool_name": "...", "arguments": {...}}``.

        Args:
            calls: Ordered list of tool calls to execute.
            stop_on_error: Stop immediately after the first failed call.
        """
        return _dumps(await catalog.run_batch(calls, ctx, stop_on_error=stop_on_error))

    for gateway in (houdini_discover, houdini_schema, houdini_call, houdini_batch):
        server.add_tool(gateway)
        forbid_unknown_arguments(server, gateway.__name__)


__all__ = [
    "GATEWAY_TOOL_NAMES",
    "HOOK_MARKER",
    "LEGACY_EXPOSURE_ENV",
    "apply_progressive",
    "build_instructions",
    "check_legacy_mode",
    "consume_cli_args",
    "current_catalog",
    "install",
    "register_gateway_tools",
    "restore_full",
]
