Houdini MCP, progressive gateway mode: the {tool_count} tools across {category_count} categories referred to below are not exposed directly. Reach them through 4 gateway tools:
- `houdini_discover(query?, category?, limit?)`: list categories, or search tools by keywords
- `houdini_schema(tool_name | tool_names)`: full docstring and parameter schema
- `houdini_call(tool_name, arguments?)`: run one tool; validation errors include its docstring and schema
- `houdini_batch(calls, stop_on_error?)`: run several calls in order, in one round-trip

Categories: {categories}.
Wherever the rules below name a tool `X(...)`, call `houdini_call("X", {{...}})` or put it in `houdini_batch`. Check its schema with `houdini_schema` first if unsure of the arguments.
