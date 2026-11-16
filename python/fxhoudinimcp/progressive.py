"""Progressive-disclosure gateway and internal tool catalog.

In ``progressive`` exposure mode the server advertises only four gateway tools
(``houdini_discover``, ``houdini_schema``, ``houdini_call``, ``houdini_batch``)
instead of all 200+ Houdini tools up front.  Every Houdini tool is still
registered into an internal mirror ``ToolManager`` inside :class:`ToolCatalog`,
preserving ``forbid_unknown_arguments``, ``compact_json`` serialization, and
MCP ``Context`` injection.
"""

from __future__ import annotations

# Built-in
import contextlib
import functools
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

# Third-party
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Internal
from fxhoudinimcp._sdk import (
    Context,
    ToolError,
    ToolManager,
    UnexpectedToolError,
    forbid_unknown_arguments,
    forbid_unknown_arguments_for_manager,
)


@dataclass(frozen=True)
class CatalogEntry:
    """Metadata and executable SDK Tool record for one Houdini tool."""

    name: str
    category: str
    summary: str
    description: str
    tool: Any


class BatchCallItem(BaseModel):
    """Single step in a ``houdini_batch`` request."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(description="Registered Houdini tool name.")
    arguments: dict[str, Any] | None = Field(
        default=None,
        description="Keyword arguments for the tool.",
    )


class ToolCatalog:
    """Internal mirror ``ToolManager`` storing all Houdini tools for gateway dispatch."""

    def __init__(self) -> None:
        self._manager = ToolManager(warn_on_duplicate_tools=False)
        self._entries: dict[str, CatalogEntry] = {}
        self._categories: dict[str, list[str]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def register_tool(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: Any = None,
        icons: Any = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
        category: str | None = None,
    ) -> Any:
        """Register *fn* in the internal mirror ToolManager and index its metadata."""
        tool_name = name or fn.__name__
        self._manager.add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )
        forbid_unknown_arguments_for_manager(self._manager, tool_name)
        tool = self._manager.get_tool(tool_name)
        cat = category or fn.__module__.rsplit(".", 1)[-1]
        doc = (getattr(tool, "description", None) or inspect.getdoc(fn) or "").strip()
        summary = next((line.strip() for line in doc.splitlines() if line.strip()), "")

        self._entries[tool_name] = CatalogEntry(
            name=tool_name,
            category=cat,
            summary=summary,
            description=doc,
            tool=tool,
        )
        bucket = self._categories.setdefault(cat, [])
        if tool_name not in bucket:
            bucket.append(tool_name)
        return tool

    def get(self, name: str) -> CatalogEntry | None:
        """Return the :class:`CatalogEntry` for *name*, or ``None``."""
        return self._entries.get(name)

    def names(self) -> list[str]:
        """All registered Houdini tool names in registration order."""
        return list(self._entries)

    def categories(self) -> dict[str, list[str]]:
        """Mapping of category name to registered tool names."""
        return {cat: list(names) for cat, names in self._categories.items()}

    def tools_map(self) -> dict[str, Any]:
        """Mapping of tool name to SDK ``Tool`` object."""
        return {name: entry.tool for name, entry in self._entries.items()}

    def _suggestions(self, name: str, limit: int = 5) -> list[str]:
        all_names = list(self._entries)
        lowered = name.lower()
        partial = [n for n in all_names if lowered in n.lower()]
        close = get_close_matches(name, all_names, n=limit, cutoff=0.45)
        ordered: list[str] = []
        for candidate in (*close, *partial):
            if candidate not in ordered:
                ordered.append(candidate)
            if len(ordered) >= limit:
                break
        return ordered

    def discover(
        self,
        query: str | None = None,
        category: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Browse categories or search registered Houdini tools."""
        if limit < 1:
            raise ToolError("limit must be at least 1.")
        effective_limit = min(limit, 250)

        cat_key: str | None = None
        if category is not None:
            cat_key = category.strip().lower()
            if cat_key not in self._categories:
                available = ", ".join(sorted(self._categories))
                raise ToolError(
                    f"Unknown category '{category}'. Available categories: {available}."
                )

        if query is None and cat_key is None:
            return {
                "total_tools": len(self._entries),
                "total_categories": len(self._categories),
                "categories": [
                    {
                        "category": cat,
                        "count": len(names),
                        "tools": list(names),
                    }
                    for cat, names in sorted(self._categories.items())
                ],
            }

        candidate_names = self._categories[cat_key] if cat_key is not None else list(self._entries)
        tokens = query.strip().lower().split() if query and query.strip() else []
        raw_query = query.strip().lower() if query and query.strip() else ""

        ranked: list[tuple[int, str, str, CatalogEntry]] = []
        for name in candidate_names:
            entry = self._entries[name]
            if not tokens:
                ranked.append((0, entry.category, entry.name, entry))
                continue

            props = entry.tool.parameters.get("properties", {})
            param_blob = " ".join(props.keys()).lower()
            name_low = entry.name.lower()
            cat_low = entry.category.lower()
            summary_low = entry.summary.lower()
            doc_low = entry.description.lower()
            haystack = f"{name_low} {cat_low} {param_blob} {doc_low}"

            if not all(token in haystack for token in tokens):
                continue

            if name_low == raw_query:
                score = 0
            elif name_low.startswith(raw_query):
                score = 1
            elif raw_query in name_low:
                score = 2
            elif all(token in name_low for token in tokens):
                score = 3
            elif any(
                token in summary_low or token in param_blob or token in cat_low for token in tokens
            ):
                score = 4
            else:
                score = 5
            ranked.append((score, entry.category, entry.name, entry))

        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        total_matches = len(ranked)
        shown = []
        for _, _, _, entry in ranked[:effective_limit]:
            props = entry.tool.parameters.get("properties", {})
            required = list(entry.tool.parameters.get("required", []))
            required_set = set(required)
            optional = [k for k in props if k not in required_set]
            shown.append(
                {
                    "name": entry.name,
                    "category": entry.category,
                    "summary": entry.summary,
                    "required_args": required,
                    "optional_args": optional,
                }
            )

        return {
            "total_matches": total_matches,
            "returned": len(shown),
            "truncated": total_matches > len(shown),
            "tools": shown,
        }

    def schema_for(
        self,
        tool_name: str | None = None,
        tool_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return full docstrings and JSON schemas for one or more tools."""
        if tool_name is None and not tool_names:
            raise ToolError("Pass tool_name or tool_names.")

        if tool_name is not None and tool_names is None:
            entry = self.get(tool_name)
            if entry is None:
                suggestions = self._suggestions(tool_name)
                hint = f" Did you mean: {suggestions}?" if suggestions else ""
                raise ToolError(
                    f"Unknown Houdini tool '{tool_name}'.{hint} "
                    "Use houdini_discover to find available tools."
                )
            return {
                "name": entry.name,
                "category": entry.category,
                "description": entry.description,
                "input_schema": entry.tool.parameters,
            }

        requested: list[str] = []
        if tool_name is not None:
            requested.append(tool_name)
        for name in tool_names or ():
            if name not in requested:
                requested.append(name)

        found: dict[str, Any] = {}
        missing: dict[str, Any] = {}
        for name in requested:
            entry = self.get(name)
            if entry is None:
                missing[name] = {
                    "error": f"Unknown Houdini tool '{name}'.",
                    "suggestions": self._suggestions(name),
                }
            else:
                found[name] = {
                    "name": entry.name,
                    "category": entry.category,
                    "description": entry.description,
                    "input_schema": entry.tool.parameters,
                }

        if not found and missing:
            first_name = next(iter(missing))
            suggestions = missing[first_name]["suggestions"]
            hint = f" Did you mean: {suggestions}?" if suggestions else ""
            raise ToolError(
                f"Unknown Houdini tool(s): {', '.join(missing)}.{hint} "
                "Use houdini_discover to find available tools."
            )

        payload: dict[str, Any] = {"tools": found}
        if missing:
            payload["missing"] = missing
        return payload

    def _format_validation_failure(self, entry: CatalogEntry, err_msg: str) -> str:
        schema_json = json.dumps(entry.tool.parameters, separators=(",", ":"), ensure_ascii=False)
        return (
            f"Argument validation failed for '{entry.name}': {err_msg}\n\n"
            f"Tool '{entry.name}' docstring:\n{entry.description}\n\n"
            f"Tool '{entry.name}' schema:\n{schema_json}"
        )

    def _unknown_tool_error(self, tool_name: str) -> ToolError:
        suggestions = self._suggestions(tool_name)
        hint = f" Did you mean: {suggestions}?" if suggestions else ""
        return ToolError(
            f"Unknown Houdini tool '{tool_name}'.{hint} "
            "Use houdini_discover to find available tools."
        )

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        ctx: Context,
    ) -> Any:
        """Validate and execute *tool_name* with *arguments* and *ctx*."""
        entry = self.get(tool_name)
        if entry is None:
            raise self._unknown_tool_error(tool_name)

        args = {} if arguments is None else arguments
        try:
            return await self._manager.call_tool(tool_name, args, ctx, convert_result=False)
        except ValidationError as exc:
            raise ToolError(self._format_validation_failure(entry, str(exc))) from exc
        except ToolError as exc:
            if UnexpectedToolError and isinstance(exc, UnexpectedToolError):
                cause = exc.__cause__
                if cause is not None:
                    raise ToolError(f"{tool_name} failed: {cause}") from cause
                raise
            if isinstance(exc.__cause__, ValidationError):
                raise ToolError(
                    self._format_validation_failure(entry, str(exc.__cause__))
                ) from exc.__cause__
            raise
        except Exception as exc:
            raise ToolError(f"{tool_name} failed: {exc}") from exc

    async def run_batch(
        self,
        calls: list[BatchCallItem],
        ctx: Context,
        *,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Execute *calls* sequentially against the catalog."""
        results: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        stopped_early = False

        for index, item in enumerate(calls):
            entry = self.get(item.tool_name)
            if entry is None:
                failed += 1
                results.append(
                    {
                        "index": index,
                        "tool_name": item.tool_name,
                        "ok": False,
                        "error": f"Unknown Houdini tool '{item.tool_name}'.",
                        "suggestions": self._suggestions(item.tool_name),
                    }
                )
                if stop_on_error:
                    stopped_early = (index + 1) < len(calls)
                    break
                continue

            args = {} if item.arguments is None else item.arguments
            try:
                raw = await self._manager.call_tool(item.tool_name, args, ctx, convert_result=False)
            except ValidationError as exc:
                failed += 1
                results.append(
                    {
                        "index": index,
                        "tool_name": item.tool_name,
                        "ok": False,
                        "error": str(exc),
                        "docstring": entry.description,
                        "input_schema": entry.tool.parameters,
                    }
                )
            except ToolError as exc:
                failed += 1
                if UnexpectedToolError and isinstance(exc, UnexpectedToolError):
                    cause = exc.__cause__ or exc
                    results.append(
                        {
                            "index": index,
                            "tool_name": item.tool_name,
                            "ok": False,
                            "error": str(cause),
                        }
                    )
                elif isinstance(exc.__cause__, ValidationError):
                    results.append(
                        {
                            "index": index,
                            "tool_name": item.tool_name,
                            "ok": False,
                            "error": str(exc.__cause__),
                            "docstring": entry.description,
                            "input_schema": entry.tool.parameters,
                        }
                    )
                else:
                    results.append(
                        {
                            "index": index,
                            "tool_name": item.tool_name,
                            "ok": False,
                            "error": str(exc),
                        }
                    )
            except Exception as exc:
                failed += 1
                results.append(
                    {
                        "index": index,
                        "tool_name": item.tool_name,
                        "ok": False,
                        "error": str(exc),
                    }
                )
            else:
                succeeded += 1
                decoded = raw
                if isinstance(raw, str):
                    with contextlib.suppress(json.JSONDecodeError):
                        decoded = json.loads(raw)
                results.append(
                    {
                        "index": index,
                        "tool_name": item.tool_name,
                        "ok": True,
                        "result": decoded,
                    }
                )
                continue

            if stop_on_error:
                stopped_early = (index + 1) < len(calls)
                break

        return {
            "total": len(calls),
            "executed": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "stopped_early": stopped_early,
            "results": results,
        }


def register_gateway_tools(
    server: Any,
    sdk_tool: Callable[..., Any],
    catalog: ToolCatalog,
    compact_serializer: Callable[[dict], str],
) -> None:
    """Register the four progressive-disclosure gateway tools on *server*."""

    def _register(fn: Callable[..., Any]) -> None:
        @functools.wraps(fn)
        async def compact(*call_args, **call_kwargs):
            result = await fn(*call_args, **call_kwargs)
            return compact_serializer(result) if isinstance(result, dict) else result

        with contextlib.suppress(Exception):
            compact.__signature__ = inspect.signature(fn, eval_str=True)
        sdk_tool()(compact)
        forbid_unknown_arguments(server, fn.__name__)

    async def houdini_discover(
        query: str | None = None,
        category: str | None = None,
        limit: int = 30,
    ) -> dict:
        """Browse categories or search all Houdini MCP tools without loading schemas.

        Call with no arguments to list all 24 categories and their tool names.
        Pass ``category`` and/or space-separated ``query`` keywords to view
        tool summaries and required/optional argument names.

        Args:
            query: Optional space-separated keywords matched against tool names,
                categories, parameter names, and descriptions.
            category: Optional category name (e.g. "nodes", "lops", "geometry").
            limit: Maximum number of matching tools to return (1-250).
        """
        return catalog.discover(query=query, category=category, limit=limit)

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
        return catalog.schema_for(tool_name=tool_name, tool_names=tool_names)

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
        return await catalog.call(tool_name, arguments, ctx)

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
        return await catalog.run_batch(calls, ctx, stop_on_error=stop_on_error)

    for gateway_fn in (houdini_discover, houdini_schema, houdini_call, houdini_batch):
        _register(gateway_fn)
