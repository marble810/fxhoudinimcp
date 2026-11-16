"""Tests for progressive-disclosure 4-tool gateway mode."""

from __future__ import annotations

# Built-in
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# Third-party
import pytest

# Internal
import fxhoudinimcp.tools  # noqa: F401  (registers all Houdini tools into catalog)
from fxhoudinimcp import __main__ as entry
from fxhoudinimcp import server
from fxhoudinimcp._loader import load_markdown
from fxhoudinimcp._sdk import ToolError
from fxhoudinimcp.config import exposure_mode
from fxhoudinimcp.errors import HoudiniCommandError

_TYPED_KEYS = {"type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const"}
_GATEWAY_TOOLS = {"houdini_discover", "houdini_schema", "houdini_call", "houdini_batch"}


def _tool_schema(tool) -> dict:
    schema = getattr(tool, "input_schema", None)
    return schema if schema is not None else tool.inputSchema


def _assert_typed(schema: object, path: str) -> None:
    if isinstance(schema, bool):
        return
    assert isinstance(schema, dict), f"{path}: expected a schema, got {schema!r}"
    if not _TYPED_KEYS & schema.keys():
        pytest.fail(f"Untyped schema at {path}: {schema}")
    for key in ("anyOf", "oneOf", "allOf"):
        for index, sub in enumerate(schema.get(key, [])):
            _assert_typed(sub, f"{path}.{key}[{index}]")
    for name, sub in schema.get("properties", {}).items():
        _assert_typed(sub, f"{path}.properties.{name}")
    for name, sub in schema.get("$defs", {}).items():
        _assert_typed(sub, f"{path}.$defs.{name}")
    if isinstance(schema.get("items"), dict):
        _assert_typed(schema["items"], f"{path}.items")
    if isinstance(schema.get("additionalProperties"), dict):
        _assert_typed(schema["additionalProperties"], f"{path}.additionalProperties")


def _extract_text(result) -> str:
    """Extract the text payload from an SDK call_tool return value."""
    content = getattr(result, "content", result)
    if isinstance(content, list):
        return "".join(getattr(part, "text", str(part)) for part in content)
    return str(content)


@pytest.fixture
def progressive_mode():
    """Switch the server to progressive mode for a test, then restore full mode."""
    previous = server.current_exposure_mode()
    server.configure_exposure("progressive")
    try:
        yield server.mcp
    finally:
        server.configure_exposure(previous)


@pytest.fixture
def patched_server_bridge(monkeypatch):
    """Patch _get_bridge inside tool modules to return a controllable mock bridge."""
    bridge = AsyncMock()
    bridge.execute = AsyncMock(return_value={"ok": True})
    bridge.health_check = AsyncMock(return_value={"status": "ok", "houdini_version": "21.0.440"})

    # Every tool module imported `_get_bridge` directly from server, so patch
    # both server._get_bridge and each loaded tool module's reference.
    monkeypatch.setattr(server, "_get_bridge", lambda _ctx: bridge)
    for mod in list(sys.modules.values()):
        if (
            mod is not None
            and getattr(mod, "__name__", "").startswith("fxhoudinimcp.tools.")
            and hasattr(mod, "_get_bridge")
        ):
            monkeypatch.setattr(mod, "_get_bridge", lambda _ctx: bridge)
    return bridge


class TestExposureConfigAndCLI:
    def test_default_exposure_is_full(self, monkeypatch):
        monkeypatch.delenv("FXHOUDINIMCP_EXPOSURE", raising=False)
        assert exposure_mode() == "full"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("full", "full"), ("FULL", "full"), (" progressive ", "progressive")],
    )
    def test_env_var_exposure_modes(self, monkeypatch, raw, expected):
        monkeypatch.setenv("FXHOUDINIMCP_EXPOSURE", raw)
        assert exposure_mode() == expected

    def test_invalid_env_var_raises(self, monkeypatch):
        monkeypatch.setenv("FXHOUDINIMCP_EXPOSURE", "minimal")
        with pytest.raises(ValueError, match="Invalid FXHOUDINIMCP_EXPOSURE"):
            exposure_mode()

    def test_cli_exposure_progressive_and_restores(self, monkeypatch):
        started: dict[str, str] = {}
        monkeypatch.setattr(
            server.mcp, "run", lambda transport: started.update(transport=transport)
        )
        monkeypatch.setenv("FXHOUDINIMCP_EXPOSURE", "full")
        monkeypatch.setattr(sys, "argv", ["fxhoudinimcp", "--exposure", "progressive"])

        try:
            entry.main()
            assert started == {"transport": "stdio"}
            assert server.current_exposure_mode() == "progressive"
        finally:
            server.configure_exposure("full")

    def test_cli_exposure_equals_syntax(self, monkeypatch):
        started: dict[str, str] = {}
        monkeypatch.setattr(
            server.mcp, "run", lambda transport: started.update(transport=transport)
        )
        monkeypatch.setattr(sys, "argv", ["fxhoudinimcp", "--exposure=progressive"])

        try:
            entry.main()
            assert started == {"transport": "stdio"}
            assert server.current_exposure_mode() == "progressive"
        finally:
            server.configure_exposure("full")

    @pytest.mark.parametrize(
        "args",
        [["--exposure"], ["--exposure", "bogus"], ["--exposure=bogus"]],
    )
    def test_cli_invalid_exposure_exits_2(self, monkeypatch, capsys, args):
        monkeypatch.setattr(sys, "argv", ["fxhoudinimcp", *args])
        with pytest.raises(SystemExit) as exc_info:
            entry.main()
        assert exc_info.value.code == 2
        assert capsys.readouterr().err


class TestExposureSwitchingAndInstructions:
    @pytest.mark.asyncio
    async def test_progressive_exposes_exactly_4_gateway_tools(self, progressive_mode):
        tools = await progressive_mode.list_tools()
        assert {t.name for t in tools} == _GATEWAY_TOOLS
        assert len(server.catalog) >= 216

        for tool in tools:
            schema = _tool_schema(tool)
            _assert_typed(schema, tool.name)
            assert schema.get("additionalProperties") is False

    @pytest.mark.asyncio
    async def test_switch_back_to_full_restores_all_tools_and_instructions(self):
        full_count = len(server.catalog)
        server.configure_exposure("progressive")
        try:
            assert len(await server.mcp.list_tools()) == 4
            assert "progressive gateway" in (server.mcp.instructions or "").lower()
        finally:
            server.configure_exposure("full")

        assert len(await server.mcp.list_tools()) == full_count
        assert "progressive gateway" not in (server.mcp.instructions or "").lower()

    def test_progressive_instructions_fit_cap_and_cover_all_24_categories(self, monkeypatch):
        modules = {
            path.stem
            for path in Path(fxhoudinimcp.tools.__file__).resolve().parent.glob("*.py")
            if path.stem != "__init__"
        }
        assert len(modules) == 24

        for auto_layout in ("0", "1"):
            monkeypatch.setenv("FXHOUDINIMCP_AUTO_LAYOUT", auto_layout)
            text = load_markdown("instructions/server_instructions_progressive.md")
            assert len(text) <= 2000, f"{len(text)} chars with AUTO_LAYOUT={auto_layout}"
            assert not re.findall(r"\{[a-z_]+\}", text)
            for gateway in _GATEWAY_TOOLS:
                assert gateway in text
            for category in modules:
                assert category in text
            assert f"{len(server.catalog)} tools across {len(modules)} categories" in text

    def test_fresh_subprocess_respects_env_progressive(self):
        env = dict(os.environ)
        env["FXHOUDINIMCP_EXPOSURE"] = "progressive"
        code = (
            "import asyncio, json, fxhoudinimcp.tools, fxhoudinimcp.server as s\n"
            "names = sorted(t.name for t in asyncio.run(s.mcp.list_tools()))\n"
            "print(json.dumps({'tools': names, 'catalog': len(s.catalog), 'mode': s.current_exposure_mode()}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout.strip())
        assert payload["mode"] == "progressive"
        assert payload["tools"] == sorted(_GATEWAY_TOOLS)
        assert payload["catalog"] >= 216


class TestGatewayDiscoverAndSchema:
    @pytest.mark.asyncio
    async def test_discover_lists_all_24_categories(self, progressive_mode):
        raw = await progressive_mode.call_tool("houdini_discover", {})
        data = json.loads(_extract_text(raw))
        assert data["total_tools"] == len(server.catalog)
        assert data["total_categories"] == 24
        assert len(data["categories"]) == 24
        categories = {entry["category"]: entry for entry in data["categories"]}
        assert "create_node" in categories["nodes"]["tools"]
        assert "build_network" in categories["graph"]["tools"]

    @pytest.mark.asyncio
    async def test_discover_filters_by_category_and_query(self, progressive_mode):
        raw = await progressive_mode.call_tool(
            "houdini_discover", {"category": "nodes", "query": "create"}
        )
        data = json.loads(_extract_text(raw))
        names = [item["name"] for item in data["tools"]]
        assert "create_node" in names
        create_node = next(item for item in data["tools"] if item["name"] == "create_node")
        assert create_node["required_args"] == ["parent_path", "node_type"]
        assert "name" in create_node["optional_args"]

    @pytest.mark.asyncio
    async def test_discover_respects_limit_and_unknown_category(self, progressive_mode):
        raw = await progressive_mode.call_tool("houdini_discover", {"query": "usd", "limit": 2})
        data = json.loads(_extract_text(raw))
        assert data["returned"] == 2
        assert data["total_matches"] > 2
        assert data["truncated"] is True

        with pytest.raises(ToolError, match="Unknown category 'nope'"):
            await progressive_mode.call_tool("houdini_discover", {"category": "nope"})

    @pytest.mark.asyncio
    async def test_schema_single_and_batch(self, progressive_mode):
        single_raw = await progressive_mode.call_tool(
            "houdini_schema", {"tool_name": "create_node"}
        )
        single = json.loads(_extract_text(single_raw))
        assert single["name"] == "create_node"
        assert single["category"] == "nodes"
        assert "Create a node" in single["description"]
        assert single["input_schema"]["additionalProperties"] is False

        batch_raw = await progressive_mode.call_tool(
            "houdini_schema", {"tool_names": ["create_node", "verify_network", "missing_tool"]}
        )
        batch = json.loads(_extract_text(batch_raw))
        assert set(batch["tools"]) == {"create_node", "verify_network"}
        assert "missing_tool" in batch["missing"]

    @pytest.mark.asyncio
    async def test_schema_unknown_tool_suggests_close_match(self, progressive_mode):
        with pytest.raises(ToolError, match="create_node"):
            await progressive_mode.call_tool("houdini_schema", {"tool_name": "creat_node"})


class TestGatewayCallAndBatch:
    @pytest.mark.asyncio
    async def test_call_delegates_with_context_and_compact_json(
        self, progressive_mode, patched_server_bridge
    ):
        patched_server_bridge.execute.return_value = {
            "path": "/obj/geo1/box1",
            "bbox": [1.234567890123, -10.388985633850098],
        }
        raw = await progressive_mode.call_tool(
            "houdini_call",
            {
                "tool_name": "create_node",
                "arguments": {"parent_path": "/obj/geo1", "node_type": "box"},
            },
        )
        text = _extract_text(raw)
        # Compact JSON: no indentation whitespace and 7-sig-fig float trimming.
        assert "\n" not in text
        assert json.loads(text) == {"path": "/obj/geo1/box1", "bbox": [1.234568, -10.38899]}
        patched_server_bridge.execute.assert_awaited_once_with(
            "nodes.create_node",
            {"parent_path": "/obj/geo1", "node_type": "box"},
        )

    @pytest.mark.asyncio
    async def test_call_zero_arg_tool_without_arguments(
        self, progressive_mode, patched_server_bridge
    ):
        patched_server_bridge.execute.return_value = {"hip_file": "shot.hip"}
        raw = await progressive_mode.call_tool("houdini_call", {"tool_name": "get_scene_info"})
        assert json.loads(_extract_text(raw)) == {"hip_file": "shot.hip"}
        patched_server_bridge.execute.assert_awaited_once_with("scene.get_scene_info")

    @pytest.mark.asyncio
    async def test_call_validation_failure_includes_docstring_and_schema(
        self, progressive_mode, patched_server_bridge
    ):
        with pytest.raises(ToolError) as exc_info:
            await progressive_mode.call_tool(
                "houdini_call",
                {
                    "tool_name": "create_node",
                    "arguments": {
                        "parent_path": "/obj",
                        "node_type": "box",
                        "node_name": "probe",
                    },
                },
            )
        msg = str(exc_info.value)
        assert "'node_name' (did you mean 'name' or 'node_type'?)" in msg
        assert "Tool 'create_node' docstring:" in msg
        assert "Create a node inside a parent network." in msg
        assert "Tool 'create_node' schema:" in msg
        assert '"additionalProperties":false' in msg
        patched_server_bridge.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_missing_required_arg_includes_docstring_and_schema(self, progressive_mode):
        with pytest.raises(ToolError) as exc_info:
            await progressive_mode.call_tool(
                "houdini_call",
                {"tool_name": "create_node", "arguments": {"parent_path": "/obj"}},
            )
        msg = str(exc_info.value)
        assert "node_type" in msg
        assert "Tool 'create_node' docstring:" in msg
        assert "Tool 'create_node' schema:" in msg

    @pytest.mark.asyncio
    async def test_call_surfaces_houdini_command_error(
        self, progressive_mode, patched_server_bridge
    ):
        patched_server_bridge.execute.side_effect = HoudiniCommandError(
            "Node not found: /obj/missing"
        )
        with pytest.raises(ToolError, match=r"get_node_info failed: Node not found: /obj/missing"):
            await progressive_mode.call_tool(
                "houdini_call",
                {"tool_name": "get_node_info", "arguments": {"node_path": "/obj/missing"}},
            )

    @pytest.mark.asyncio
    async def test_call_rejects_unknown_gateway_argument(self, progressive_mode):
        with pytest.raises(ToolError, match="bogus"):
            await progressive_mode.call_tool(
                "houdini_call",
                {"tool_name": "get_scene_info", "bogus": 1},
            )

    @pytest.mark.asyncio
    async def test_batch_sequential_success_and_stop_on_error(
        self, progressive_mode, patched_server_bridge
    ):
        patched_server_bridge.execute.side_effect = [
            {"hip_file": "a.hip"},
            {"path": "/obj/geo1"},
        ]
        raw = await progressive_mode.call_tool(
            "houdini_batch",
            {
                "calls": [
                    {"tool_name": "get_scene_info"},
                    {
                        "tool_name": "create_node",
                        "arguments": {"parent_path": "/obj", "node_type": "geo"},
                    },
                    {
                        "tool_name": "create_node",
                        "arguments": {"parent_path": "/obj", "bogus": "x"},
                    },
                    {"tool_name": "get_scene_info"},
                ],
                "stop_on_error": True,
            },
        )
        data = json.loads(_extract_text(raw))
        assert data["total"] == 4
        assert data["executed"] == 3
        assert data["succeeded"] == 2
        assert data["failed"] == 1
        assert data["stopped_early"] is True
        assert data["results"][0]["result"] == {"hip_file": "a.hip"}
        assert data["results"][1]["result"] == {"path": "/obj/geo1"}
        assert data["results"][2]["ok"] is False
        assert "docstring" in data["results"][2]
        assert "input_schema" in data["results"][2]

    @pytest.mark.asyncio
    async def test_batch_continue_when_stop_on_error_false(
        self, progressive_mode, patched_server_bridge
    ):
        patched_server_bridge.execute.return_value = {"hip_file": "ok.hip"}
        raw = await progressive_mode.call_tool(
            "houdini_batch",
            {
                "calls": [
                    {"tool_name": "nonexistent_tool"},
                    {"tool_name": "get_scene_info"},
                ],
                "stop_on_error": False,
            },
        )
        data = json.loads(_extract_text(raw))
        assert data["total"] == 2
        assert data["executed"] == 2
        assert data["succeeded"] == 1
        assert data["failed"] == 1
        assert data["stopped_early"] is False
        assert data["results"][0]["ok"] is False
        assert data["results"][1]["ok"] is True
        assert data["results"][1]["result"] == {"hip_file": "ok.hip"}

    @pytest.mark.asyncio
    async def test_batch_rejects_unknown_item_key(self, progressive_mode):
        with pytest.raises(ToolError, match="Extra inputs are not permitted|unknown_key"):
            await progressive_mode.call_tool(
                "houdini_batch",
                {"calls": [{"tool_name": "get_scene_info", "unknown_key": 1}]},
            )
