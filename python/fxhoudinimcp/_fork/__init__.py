"""Fork-only features layered on top of upstream fxhoudinimcp.

Everything in this package belongs to the fork (marble810/fxhoudinimcp) and is
never touched by upstream (healkeiser/fxhoudinimcp). Upstream modules must not
import from here; the only link is a two-line hook at the top of
``fxhoudinimcp.__main__.main`` marked ``# fork:progressive``.

Module map:

* ``upstream_api``  -- the ONLY place that imports upstream fxhoudinimcp code or
  touches private MCP SDK attributes. When an upstream merge breaks the fork,
  the fix almost always belongs here.
* ``catalog``       -- the internal tool catalog the gateway dispatches into.
* ``progressive``   -- exposure-mode parsing, the CLI hook, and the four
  gateway tools.
* ``preamble.md``   -- the gateway preamble prepended to upstream's own server
  instructions in progressive mode.

See ``.fork/AGENT.md`` at the repository root for the upstream-merge procedure.
"""
