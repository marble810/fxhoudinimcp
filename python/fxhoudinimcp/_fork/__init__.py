"""Fork-only features layered on top of upstream fxhoudinimcp.

Everything in this package belongs to the fork (marble810/fxhoudinimcp) and is
never touched by upstream (healkeiser/fxhoudinimcp). Upstream modules must not
import from here; the only link is the hook at the top of
``fxhoudinimcp.__main__.main`` (lines marked ``# fork:progressive``) that calls
:func:`install_hook`.

Module map:

* ``upstream_api``  -- the ONLY place that imports upstream fxhoudinimcp code or
  touches private MCP SDK attributes. When an upstream merge breaks the fork,
  the fix almost always belongs here.
* ``catalog``       -- the internal tool catalog the gateway dispatches into.
* ``progressive``   -- exposure-mode parsing, the CLI hook, and the four
  gateway tools.
* ``preamble.md``   -- the gateway preamble prepended to upstream's own server
  instructions in progressive mode.

This file must stay dependency-free (stdlib only): it is what keeps upstream's
CLI working even when the rest of the fork no longer imports.

See ``.fork/AGENT.md`` at the repository root for the upstream-merge procedure.
"""

from __future__ import annotations

import os
import sys

_ENV = "FXHOUDINIMCP_EXPOSURE"


def install_hook(argv: list[str] | None = None) -> None:
    """Entry-point hook. Never lets a broken fork take upstream's CLI down.

    If the fork fails to import (an upstream change it has not caught up
    with), ``full`` mode still starts upstream unchanged, and only an explicit
    request for ``progressive`` fails, with exit code 2 and the reason.
    """
    try:
        from fxhoudinimcp._fork import progressive
    except Exception as exc:  # noqa: BLE001 - any import failure means "fork unavailable"
        _fallback(sys.argv if argv is None else argv, exc)
        return
    progressive.install(argv)


def _fallback(argv: list[str], exc: Exception) -> None:
    requested = os.getenv(_ENV, "")
    remaining = [argv[0]] if argv else []
    args = argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--exposure":
            requested = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if arg.startswith("--exposure="):
            requested = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    argv[:] = remaining

    if requested.strip().lower() not in ("", "full"):
        print(
            f"progressive exposure mode is unavailable in this build: {exc!r}\n"
            "Start without --exposure / FXHOUDINIMCP_EXPOSURE to run the full tool set.\n",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"fxhoudinimcp fork extensions disabled: {exc!r}", file=sys.stderr)
