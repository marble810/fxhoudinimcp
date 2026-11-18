"""Internal catalog of upstream Houdini tools for the progressive gateway.

The catalog holds the very same SDK ``Tool`` objects upstream registered on
the server (already wrapped for compact JSON and strict arguments), so the
gateway executes exactly what full mode would. Nothing here knows any
individual upstream tool by name.
"""

from __future__ import annotations

# Built-in
import contextlib
import json
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

# Third-party
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Internal
from fxhoudinimcp._fork.upstream_api import (
    Context,
    ToolError,
    UnexpectedToolError,
    new_tool_manager,
    tool_category,
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


def _wraps_tool_exception(exc: BaseException) -> bool:
    """Whether the SDK wrapped an exception the tool itself raised.

    mcp 2.x raises ``UnexpectedToolError``; 1.x raises a plain ``ToolError``
    ("Error executing tool X: ...") chained to the original exception. A
    ``ToolError`` the tool raised on purpose has no foreign cause.
    """
    cause = exc.__cause__
    if cause is None:
        return False
    if UnexpectedToolError is not None and isinstance(exc, UnexpectedToolError):
        return True
    return not isinstance(cause, ToolError | ValidationError)


class ToolCatalog:
    """Read-only index plus a private ToolManager used for dispatch."""

    def __init__(self, server: Any, tools: list[Any]) -> None:
        self._manager = new_tool_manager(server, tools)
        self._entries: dict[str, CatalogEntry] = {}
        self._categories: dict[str, list[str]] = {}
        for tool in tools:
            self._index(tool)

    def _index(self, tool: Any) -> None:
        category = tool_category(tool)
        doc = (getattr(tool, "description", None) or "").strip()
        summary = next((line.strip() for line in doc.splitlines() if line.strip()), "")
        self._entries[tool.name] = CatalogEntry(
            name=tool.name,
            category=category,
            summary=summary,
            description=doc,
            tool=tool,
        )
        bucket = self._categories.setdefault(category, [])
        if tool.name not in bucket:
            bucket.append(tool.name)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, name: str) -> CatalogEntry | None:
        """Return the :class:`CatalogEntry` for *name*, or ``None``."""
        return self._entries.get(name)

    def names(self) -> list[str]:
        """All tool names in registration order."""
        return list(self._entries)

    def categories(self) -> dict[str, list[str]]:
        """Mapping of category name to tool names."""
        return {cat: list(names) for cat, names in self._categories.items()}

    def tools(self) -> list[Any]:
        """The SDK ``Tool`` objects, in registration order."""
        return [entry.tool for entry in self._entries.values()]

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

    def _unknown_tool_error(self, tool_name: str) -> ToolError:
        suggestions = self._suggestions(tool_name)
        hint = f" Did you mean: {suggestions}?" if suggestions else ""
        return ToolError(
            f"Unknown Houdini tool '{tool_name}'.{hint} "
            "Use houdini_discover to find available tools."
        )

    # -- discover ---------------------------------------------------------

    def discover(
        self,
        query: str | None = None,
        category: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Browse categories or search tools by keywords."""
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
                    {"category": cat, "count": len(names), "tools": list(names)}
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
            shown.append(
                {
                    "name": entry.name,
                    "category": entry.category,
                    "summary": entry.summary,
                    "required_args": required,
                    "optional_args": [k for k in props if k not in required_set],
                }
            )

        return {
            "total_matches": total_matches,
            "returned": len(shown),
            "truncated": total_matches > len(shown),
            "tools": shown,
        }

    # -- schema -----------------------------------------------------------

    def _schema_payload(self, entry: CatalogEntry) -> dict[str, Any]:
        return {
            "name": entry.name,
            "category": entry.category,
            "description": entry.description,
            "input_schema": entry.tool.parameters,
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
                raise self._unknown_tool_error(tool_name)
            return self._schema_payload(entry)

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
                found[name] = self._schema_payload(entry)

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

    # -- call / batch -----------------------------------------------------

    def _format_validation_failure(self, entry: CatalogEntry, err_msg: str) -> str:
        schema_json = json.dumps(entry.tool.parameters, separators=(",", ":"), ensure_ascii=False)
        return (
            f"Argument validation failed for '{entry.name}': {err_msg}\n\n"
            f"Tool '{entry.name}' docstring:\n{entry.description}\n\n"
            f"Tool '{entry.name}' schema:\n{schema_json}"
        )

    async def call(self, tool_name: str, arguments: dict[str, Any] | None, ctx: Context) -> Any:
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
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                raise ToolError(self._format_validation_failure(entry, str(cause))) from cause
            if _wraps_tool_exception(exc):
                raise ToolError(f"{tool_name} failed: {cause}") from cause
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
            base = {"index": index, "tool_name": item.tool_name}
            entry = self.get(item.tool_name)
            if entry is None:
                failed += 1
                results.append(
                    {
                        **base,
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
            validation_detail = {
                "docstring": entry.description,
                "input_schema": entry.tool.parameters,
            }
            try:
                raw = await self._manager.call_tool(item.tool_name, args, ctx, convert_result=False)
            except ValidationError as exc:
                failed += 1
                results.append({**base, "ok": False, "error": str(exc), **validation_detail})
            except ToolError as exc:
                failed += 1
                if isinstance(exc.__cause__, ValidationError):
                    results.append(
                        {**base, "ok": False, "error": str(exc.__cause__), **validation_detail}
                    )
                elif _wraps_tool_exception(exc):
                    results.append({**base, "ok": False, "error": str(exc.__cause__)})
                else:
                    results.append({**base, "ok": False, "error": str(exc)})
            except Exception as exc:
                failed += 1
                results.append({**base, "ok": False, "error": str(exc)})
            else:
                succeeded += 1
                decoded = raw
                if isinstance(raw, str):
                    with contextlib.suppress(json.JSONDecodeError):
                        decoded = json.loads(raw)
                results.append({**base, "ok": True, "result": decoded})
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
