"""Fork-only features layered on top of upstream fxhoudinimcp.

Everything in this package belongs to the fork (marble810/fxhoudinimcp) and is
never touched by upstream (healkeiser/fxhoudinimcp). Upstream modules must not
import from here; the only link is the hook at the top of
``fxhoudinimcp.__main__.main`` (lines marked ``# fork:progressive``) that calls
:func:`install_hook`.

The fork is progressive-only: its MCP server always exposes the four gateway
tools. For the full tool surface, use upstream.

Module map:

* ``upstream_api``  -- the ONLY place that imports upstream fxhoudinimcp code or
  touches private MCP SDK attributes. When an upstream merge breaks the fork,
  the fix almost always belongs here.
* ``catalog``       -- the internal tool catalog the gateway dispatches into.
* ``progressive``   -- the CLI hook and the four gateway tools.
* ``preamble.md``   -- the gateway preamble prepended to upstream's own server
  instructions.

This file must stay dependency-free (stdlib only): it is what keeps upstream's
non-server subcommands (install, uninstall, --help, ...) working even when the
rest of the fork no longer imports.

See ``.fork/AGENT.md`` at the repository root for the upstream-merge procedure.
"""

from __future__ import annotations

import sys


def install_hook(argv: list[str] | None = None) -> None:
    """Entry-point hook: make the server progressive-only.

    If the fork fails to import (an upstream change it has not caught up
    with), subcommands still run, but starting the server exits 2 with the
    reason instead of silently serving the full tool set.
    """
    try:
        from fxhoudinimcp._fork import progressive
    except Exception as exc:  # noqa: BLE001 - any import failure means "fork unavailable"
        _fallback(sys.argv if argv is None else argv, exc)
        return
    progressive.install(argv)


def _fallback(argv: list[str], exc: Exception) -> None:
    remaining = [argv[0]] if argv else []
    args = argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--exposure":
            index += 2
            continue
        if arg.startswith("--exposure="):
            index += 1
            continue
        remaining.append(arg)
        index += 1
    argv[:] = remaining

    if len(remaining) <= 1:  # no subcommand: upstream would start the server
        print(
            f"fxhoudinimcp (progressive fork) cannot start its gateway: {exc!r}\n"
            "This build is broken against its upstream base; reinstall a released "
            "version, or use upstream fxhoudinimcp for the full tool set.\n",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"fxhoudinimcp fork extensions unavailable: {exc!r}", file=sys.stderr)
