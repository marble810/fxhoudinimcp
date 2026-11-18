"""Tests for the fork's progressive-disclosure gateway (fxhoudinimcp._fork).

Two layers, kept deliberately independent of individual upstream tools so an
upstream rename or signature change never breaks these tests:

* ``TestGateway*``  -- a synthetic server with a handful of fake tools.
* ``TestUpstream*`` -- invariants against the real server, computed
  dynamically from whatever tools upstream currently registers.
"""

from __future__ import annotations

# Built-in
import json
import re
import sys
from pathlib import Path

# Third-party
import pytest

# Internal
from fxhoudinimcp._fork import progressive
from fxhoudinimcp._fork.upstream_api import (
    ToolError,
    forbid_unknown_arguments,
    get_instructions,
    list_tool_objects,
    sdk_module,
)
from fxhoudinimcp._sdk import Context, build_server

GATEWAYS = set(progressive.GATEWAY_TOOL_NAMES)
_TYPED_KEYS = {"type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const"}


# -- helpers -----------------------------------------------------------------


def _text(result) -> str:
    """Text payload of an SDK ``call_tool`` result on either SDK major."""
    if isinstance(result, tuple):  # mcp 1.x with structured output
        result = result[0]
    content = getattr(result, "content", result)
    if isinstance(content, list):
        return "".join(getattr(part, "text", str(part)) for part in content)
    return str(content)


async def _call_json(server, name: str, arguments: dict):
    return json.loads(_text(await server.call_tool(name, arguments)))


def _schema(tool) -> dict:
    schema = getattr(tool, "input_schema", None)
    return schema if schema is not None else tool.inputSchema


def _assert_typed(schema, path: str) -> None:
    if isinstance(schema, bool):
        return
    assert isinstance(schema, dict), f"{path}: expected a schema, got {schema!r}"
    assert _TYPED_KEYS & schema.keys(), f"Untyped schema at {path}: {schema}"
    for key in ("anyOf", "oneOf", "allOf"):
        for index, sub in enumerate(schema.get(key, [])):
            _assert_typed(sub, f"{path}.{key}[{index}]")
    for name, sub in schema.get("properties", {}).items():
        _assert_typed(sub, f"{path}.properties.{name}")
    for name, sub in schema.get("$defs", {}).items():
        _assert_typed(sub, f"{path}.$defs.{name}")
    if isinstance(schema.get("items"), dict):
        _assert_typed(schema["items"], f"{path}.items")


# -- synthetic server ----------------------------------------------------------


async def make_thing(parent: str, kind: str, name: str | None = None) -> dict:
    """Make a thing inside a parent.

    Longer explanation that only houdini_schema returns.
    """
    return {"parent": parent, "kind": kind, "name": name, "value": 1.5}


async def list_things() -> dict:
    """List every thing."""
    return {"things": [1, 2, 3]}


async def explode(reason: str) -> dict:
    """Always fails."""
    raise RuntimeError(f"boom: {reason}")


async def whoami(ctx: Context) -> dict:
    """Report whether a context was injected."""
    return {"has_ctx": ctx is not None}


_FAKE_TOOLS = {
    make_thing: "fxhoudinimcp.tools.alpha",
    list_things: "fxhoudinimcp.tools.alpha",
    explode: "fxhoudinimcp.tools.beta",
    whoami: "fxhoudinimcp.tools.beta",
}
_UPSTREAM_TEXT = "UPSTREAM RULES: call make_thing(...) before list_things()."


@pytest.fixture
def fake_server():
    server = build_server(name="fork-test", instructions=_UPSTREAM_TEXT, lifespan=None, version="0")
    for fn, module in _FAKE_TOOLS.items():
        fn.__module__ = module
        server.add_tool(fn)
        forbid_unknown_arguments(server, fn.__name__)
    return server


@pytest.fixture
def gateway(fake_server):
    progressive.apply_progressive(fake_server)
    return fake_server


class TestGatewaySurface:
    async def test_only_gateways_are_listed(self, gateway):
        tools = await gateway.list_tools()
        assert {t.name for t in tools} == GATEWAYS
        for tool in tools:
            schema = _schema(tool)
            _assert_typed(schema, tool.name)
            assert schema.get("additionalProperties") is False, tool.name

    def test_apply_is_idempotent_and_restore_round_trips(self, fake_server):
        before = [t.name for t in list_tool_objects(fake_server)]
        first = progressive.apply_progressive(fake_server)
        assert progressive.apply_progressive(fake_server) is first
        assert progressive.current_catalog(fake_server) is first
        progressive.restore_full(fake_server)
        assert [t.name for t in list_tool_objects(fake_server)] == before
        assert get_instructions(fake_server) == _UPSTREAM_TEXT
        assert progressive.current_catalog(fake_server) is None

    def test_instructions_are_preamble_plus_upstream_verbatim(self, gateway):
        text = get_instructions(gateway)
        assert text.rstrip().endswith(_UPSTREAM_TEXT)
        assert "4 tools across 2 categories" in text
        assert "Categories: alpha, beta." in text
        for name in GATEWAYS:
            assert name in text
        assert not re.findall(r"\{[a-z_]+\}", text)

    def test_refuses_empty_server(self):
        empty = build_server(name="empty", instructions="x", lifespan=None, version="0")
        with pytest.raises(RuntimeError, match="No tools are registered"):
            progressive.apply_progressive(empty)


class TestGatewayDiscoverAndSchema:
    async def test_discover_lists_categories(self, gateway):
        data = await _call_json(gateway, "houdini_discover", {})
        assert data["total_tools"] == 4
        cats = {c["category"]: c["tools"] for c in data["categories"]}
        assert cats == {"alpha": ["make_thing", "list_things"], "beta": ["explode", "whoami"]}

    async def test_discover_query_and_args(self, gateway):
        data = await _call_json(gateway, "houdini_discover", {"query": "make", "category": "alpha"})
        item = data["tools"][0]
        assert item["name"] == "make_thing"
        assert item["summary"] == "Make a thing inside a parent."
        assert item["required_args"] == ["parent", "kind"]
        assert item["optional_args"] == ["name"]

    async def test_discover_limit_and_unknown_category(self, gateway):
        data = await _call_json(gateway, "houdini_discover", {"query": "thing", "limit": 1})
        assert data["returned"] == 1 and data["truncated"] is True
        with pytest.raises(ToolError, match="Unknown category 'nope'"):
            await gateway.call_tool("houdini_discover", {"category": "nope"})

    async def test_schema_single_batch_and_suggestion(self, gateway):
        single = await _call_json(gateway, "houdini_schema", {"tool_name": "make_thing"})
        assert single["category"] == "alpha"
        assert "Longer explanation" in single["description"]
        assert single["input_schema"]["additionalProperties"] is False

        batch = await _call_json(
            gateway, "houdini_schema", {"tool_names": ["make_thing", "missing_tool"]}
        )
        assert set(batch["tools"]) == {"make_thing"}
        assert "missing_tool" in batch["missing"]

        with pytest.raises(ToolError, match="make_thing"):
            await gateway.call_tool("houdini_schema", {"tool_name": "make_thin"})


class TestGatewayCallAndBatch:
    async def test_call_runs_tool_and_returns_compact_json(self, gateway):
        raw = _text(
            await gateway.call_tool(
                "houdini_call",
                {"tool_name": "make_thing", "arguments": {"parent": "/obj", "kind": "box"}},
            )
        )
        assert "\n" not in raw and ": " not in raw
        assert json.loads(raw) == {"parent": "/obj", "kind": "box", "name": None, "value": 1.5}

    async def test_call_without_arguments_and_context_injection(self, gateway):
        assert await _call_json(gateway, "houdini_call", {"tool_name": "list_things"}) == {
            "things": [1, 2, 3]
        }
        assert await _call_json(gateway, "houdini_call", {"tool_name": "whoami"}) == {
            "has_ctx": True
        }

    async def test_call_unknown_argument_includes_hint_docstring_schema(self, gateway):
        with pytest.raises(ToolError) as exc_info:
            await gateway.call_tool(
                "houdini_call",
                {
                    "tool_name": "make_thing",
                    "arguments": {"parent": "/obj", "kind": "box", "thing_name": "x"},
                },
            )
        msg = str(exc_info.value)
        assert "'thing_name'" in msg and "'name'" in msg
        assert "Tool 'make_thing' docstring:" in msg
        assert "Tool 'make_thing' schema:" in msg
        assert '"additionalProperties":false' in msg

    async def test_call_missing_required_argument(self, gateway):
        with pytest.raises(ToolError, match="Tool 'make_thing' schema:"):
            await gateway.call_tool(
                "houdini_call", {"tool_name": "make_thing", "arguments": {"parent": "/obj"}}
            )

    async def test_call_wraps_tool_exceptions(self, gateway):
        with pytest.raises(ToolError, match="explode failed: boom: why"):
            await gateway.call_tool(
                "houdini_call", {"tool_name": "explode", "arguments": {"reason": "why"}}
            )

    async def test_call_unknown_tool_and_unknown_gateway_argument(self, gateway):
        with pytest.raises(ToolError, match="make_thing"):
            await gateway.call_tool("houdini_call", {"tool_name": "make_thign"})
        with pytest.raises(ToolError, match="bogus"):
            await gateway.call_tool("houdini_call", {"tool_name": "list_things", "bogus": 1})

    async def test_batch_stops_on_error(self, gateway):
        data = await _call_json(
            gateway,
            "houdini_batch",
            {
                "calls": [
                    {"tool_name": "list_things"},
                    {"tool_name": "make_thing", "arguments": {"parent": "/obj", "kind": "a"}},
                    {"tool_name": "make_thing", "arguments": {"parent": "/obj", "bogus": 1}},
                    {"tool_name": "list_things"},
                ]
            },
        )
        assert (data["total"], data["executed"], data["succeeded"], data["failed"]) == (4, 3, 2, 1)
        assert data["stopped_early"] is True
        assert data["results"][0]["result"] == {"things": [1, 2, 3]}
        assert "docstring" in data["results"][2] and "input_schema" in data["results"][2]

    async def test_batch_continues_when_asked(self, gateway):
        data = await _call_json(
            gateway,
            "houdini_batch",
            {
                "calls": [
                    {"tool_name": "nope"},
                    {"tool_name": "explode", "arguments": {"reason": "x"}},
                    {"tool_name": "list_things"},
                ],
                "stop_on_error": False,
            },
        )
        assert [r["ok"] for r in data["results"]] == [False, False, True]
        assert data["stopped_early"] is False
        assert "boom: x" in data["results"][1]["error"]

    async def test_batch_rejects_unknown_item_key(self, gateway):
        with pytest.raises(ToolError, match="Extra inputs are not permitted|unknown_key"):
            await gateway.call_tool(
                "houdini_batch", {"calls": [{"tool_name": "list_things", "unknown_key": 1}]}
            )


class TestLegacyExposureFlag:
    """Old client configs may still pass ``--exposure progressive``."""

    @pytest.mark.parametrize("raw", [None, "", "progressive", " PROGRESSIVE "])
    def test_progressive_or_empty_is_accepted(self, raw):
        progressive.check_legacy_mode(raw, source="X")

    def test_full_points_to_upstream(self):
        with pytest.raises(ValueError, match="pip install fxhoudinimcp"):
            progressive.check_legacy_mode("full", source="X")

    def test_unknown_is_refused(self):
        with pytest.raises(ValueError, match="progressive-only"):
            progressive.check_legacy_mode("minimal", source="X")

    @pytest.mark.parametrize(
        ("args", "remaining"),
        [
            ([], []),
            (["--exposure", "progressive"], []),
            (["--exposure=progressive"], []),
            (["--help", "--exposure", "progressive"], ["--help"]),
            (["install", "--force"], ["install", "--force"]),
        ],
    )
    def test_consume_cli_args(self, args, remaining):
        assert progressive.consume_cli_args(args) == remaining

    @pytest.mark.parametrize(
        "args",
        [
            ["--exposure"],
            ["--exposure", "bogus"],
            ["--exposure=full"],
            ["--exposure="],
            ["--exposure", "-h"],
        ],
    )
    def test_consume_cli_args_rejects(self, args):
        with pytest.raises(ValueError):
            progressive.consume_cli_args(args)


# -- the real upstream server ----------------------------------------------------


@pytest.fixture
def real_server():
    import fxhoudinimcp.tools  # noqa: F401  (registers upstream tools)
    from fxhoudinimcp.server import mcp

    return mcp


@pytest.fixture
def real_gateway(real_server):
    full_tools = list_tool_objects(real_server)
    catalog = progressive.apply_progressive(real_server)
    try:
        yield real_server, catalog, full_tools
    finally:
        progressive.restore_full(real_server)


def _tool_modules() -> set[str]:
    import fxhoudinimcp.tools

    folder = Path(fxhoudinimcp.tools.__file__).resolve().parent
    return {p.stem for p in folder.glob("*.py") if p.stem != "__init__"}


class TestUpstreamInvariants:
    def test_catalog_mirrors_full_surface_exactly(self, real_gateway):
        _, catalog, full_tools = real_gateway
        assert len(full_tools) > 100, "upstream tools did not register"
        assert catalog.names() == [t.name for t in full_tools]
        for tool in full_tools:
            entry = catalog.get(tool.name)
            assert entry.tool is tool, f"{tool.name}: catalog must reuse upstream's Tool object"
            assert entry.tool.parameters.get("additionalProperties") is False, tool.name

    def test_categories_are_upstream_tool_modules(self, real_gateway):
        _, catalog, _ = real_gateway
        assert set(catalog.categories()) == _tool_modules()

    async def test_real_server_lists_only_gateways(self, real_gateway):
        server, _, _ = real_gateway
        tools = await server.list_tools()
        assert {t.name for t in tools} == GATEWAYS
        for tool in tools:
            _assert_typed(_schema(tool), tool.name)

    def test_instructions_wrap_upstream_text(self, real_server):
        upstream_text = get_instructions(real_server)
        catalog = progressive.apply_progressive(real_server)
        try:
            text = get_instructions(real_server)
            assert upstream_text.strip() in text
            assert f"{len(catalog)} tools across {len(catalog.categories())} categories" in text
            for category in catalog.categories():
                assert category in text
        finally:
            progressive.restore_full(real_server)
        assert get_instructions(real_server) == upstream_text

    async def test_dispatch_reaches_a_real_tool(self, real_gateway, mock_ctx, mock_bridge):
        _, catalog, full_tools = real_gateway
        zero_arg = [t.name for t in full_tools if not t.parameters.get("required")]
        assert zero_arg
        for name in zero_arg:
            mock_bridge.execute.reset_mock()
            try:
                result = await catalog.call(name, None, mock_ctx)
            except ToolError:
                continue
            if mock_bridge.execute.await_count:
                assert isinstance(result, str | dict)
                return
        pytest.fail("no zero-argument upstream tool could be dispatched through the catalog")

    async def test_validation_errors_on_a_real_tool(self, real_gateway, mock_ctx):
        _, catalog, full_tools = real_gateway
        name = next(t.name for t in full_tools if t.parameters.get("required"))
        with pytest.raises(ToolError) as exc_info:
            await catalog.call(name, {}, mock_ctx)
        assert f"Tool '{name}' docstring:" in str(exc_info.value)
        assert f"Tool '{name}' schema:" in str(exc_info.value)
        with pytest.raises(ToolError, match="__fork_bogus__"):
            await catalog.call(name, {"__fork_bogus__": 1}, mock_ctx)


class TestUpstreamEntryPointHook:
    """The ``# fork:progressive`` lines in upstream ``main()`` still work."""

    @pytest.fixture
    def started(self, monkeypatch, real_server):
        record: dict = {}

        def fake_run_server(server, *, transport):
            record["transport"] = transport
            record["tools"] = {t.name for t in list_tool_objects(server)}
            record["instructions"] = get_instructions(server)

        monkeypatch.setattr(sdk_module, "run_server", fake_run_server)
        monkeypatch.delenv(progressive.LEGACY_EXPOSURE_ENV, raising=False)
        monkeypatch.delenv("MCP_TRANSPORT", raising=False)
        try:
            yield record
        finally:
            progressive.restore_full(real_server)

    def _main(self, monkeypatch, *args):
        from fxhoudinimcp import __main__ as entry

        monkeypatch.setattr(sys, "argv", ["fxhoudinimcp", *args])
        entry.main()

    def test_hook_lines_present(self):
        from fxhoudinimcp import __main__ as entry

        source = Path(entry.__file__).read_text(encoding="utf-8")
        assert source.count(progressive.HOOK_MARKER) == 2

    def test_plain_start_serves_only_the_gateway(self, monkeypatch, started, real_server):
        before = [t.name for t in list_tool_objects(real_server)]
        self._main(monkeypatch)
        assert started["tools"] == GATEWAYS
        assert started["transport"] == "stdio"
        assert "progressive gateway" in started["instructions"]
        # Upstream's own tests call main() with server.run patched; the shared
        # server must be back to normal once run_server returns.
        assert [t.name for t in list_tool_objects(real_server)] == before
        assert progressive.current_catalog(real_server) is None

    @pytest.mark.parametrize("args", [["--exposure", "progressive"], ["--exposure=progressive"]])
    def test_legacy_flag_still_starts(self, monkeypatch, started, args):
        self._main(monkeypatch, *args)
        assert started["tools"] == GATEWAYS

    def test_legacy_env_progressive_still_starts(self, monkeypatch, started):
        monkeypatch.setenv(progressive.LEGACY_EXPOSURE_ENV, "progressive")
        self._main(monkeypatch)
        assert started["tools"] == GATEWAYS

    @pytest.mark.parametrize("args", [["--exposure", "full"], ["--exposure"], ["--exposure=x"]])
    def test_other_flags_exit_2(self, monkeypatch, capsys, started, args):
        with pytest.raises(SystemExit) as exc_info:
            self._main(monkeypatch, *args)
        assert exc_info.value.code == 2
        assert "exposure" in capsys.readouterr().err
        assert not started

    @pytest.mark.parametrize("value", ["full", "minimal"])
    def test_other_env_exits_2_at_start(self, monkeypatch, started, value):
        monkeypatch.setenv(progressive.LEGACY_EXPOSURE_ENV, value)
        with pytest.raises(SystemExit) as exc_info:
            self._main(monkeypatch)
        assert exc_info.value.code == 2
        assert not started


class TestHookFallbackWhenForkIsBroken:
    """If the fork stops importing, subcommands work but the server refuses to start."""

    @pytest.fixture
    def broken_fork(self, monkeypatch):
        # A None entry in sys.modules makes `import` raise ImportError; the
        # package attribute must go too, or `from pkg import mod` finds it.
        import fxhoudinimcp._fork

        monkeypatch.setitem(sys.modules, "fxhoudinimcp._fork.progressive", None)
        monkeypatch.delattr(fxhoudinimcp._fork, "progressive")

    def test_subcommands_survive(self, broken_fork, capsys):
        from fxhoudinimcp._fork import install_hook

        argv = ["fxhoudinimcp", "--exposure", "progressive", "--help"]
        install_hook(argv)
        assert argv == ["fxhoudinimcp", "--help"]
        assert "unavailable" in capsys.readouterr().err

    @pytest.mark.parametrize("args", [[], ["--exposure", "progressive"], ["--exposure=full"]])
    def test_server_start_fails_loudly(self, broken_fork, capsys, args):
        from fxhoudinimcp._fork import install_hook

        with pytest.raises(SystemExit) as exc_info:
            install_hook(["fxhoudinimcp", *args])
        assert exc_info.value.code == 2
        assert "cannot start its gateway" in capsys.readouterr().err


def test_module_import_has_no_side_effects():
    """Importing the fork must not change the upstream server by itself."""
    code = (
        "import asyncio, fxhoudinimcp.tools\n"
        "from fxhoudinimcp.server import mcp\n"
        "n = len(asyncio.run(mcp.list_tools()))\n"
        "import fxhoudinimcp._fork.progressive\n"
        "assert len(asyncio.run(mcp.list_tools())) == n, 'import changed the tool list'\n"
    )
    import subprocess

    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
