Houdini MCP (progressive gateway): 4 tools expose 216 tools across 24 categories. Read full rules first: `houdini_call("get_workflow_guide", {{"topic": "discipline"}})`.

Gateway:
- `houdini_discover(query?, category?, limit?)`: list categories or search tools
- `houdini_schema(tool_name | tool_names)`: fetch params & docstring
- `houdini_call(tool_name, arguments?)`: run one tool (validation errors include docstring + schema)
- `houdini_batch(calls, stop_on_error?)`: run calls sequentially in one round-trip

Categories (24): scene, session, nodes, parameters, graph, geometry, vex, code, context, workflows, lops, materials, dops, tops, cops, chops, hda, animation, takes, cache, rendering, viewport, shelf, help.

Core discipline:
1. PLAN, THEN BUILD IN ONE CALL: 3+ nodes = `build_network` (`dry_run=True` for new types); batch calls (`set_parameters`, `connect_nodes_batch`, `houdini_batch`).
2. LOOK IT UP, NEVER GUESS: `get_node_card`; `search_help` + `get_help_page` (`shelf/` or workflow first); `get_workflow_guide(topic)`.
3. VERIFY, THEN CLAIM: `verify_network`; `capture_screenshot(settle_seconds=...)`. Answer questions before changing anything. Two failed fixes = read docs.
4. DRAFT, THEN UPRES with user's yes.
5. CACHE past ~3s/frame (`filecache`/`rbdio`/`vellumio`). `write_cache`/`start_render` block until done; never poll disk. Save hip first.
6. No CTRL null by default; promote knobs to HDA/subnet.
7. ISOLATE while iterating: display only what you work on.
8. NODES, NOT CODE: `build_network` > `setup_*` > `create_node` > `create_wrangle` (after `list_node_types`) > `execute_python` (never for nodes/parms/wiring).
9. Click-requiring shelf tools refuse: read script, build nodes.
10. Between steps, one concise line: done + next.
{layout_guidance}
