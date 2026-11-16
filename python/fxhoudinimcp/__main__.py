"""Entry point for the FXHoudini MCP server.

Two jobs that pull in opposite directions. With no arguments this must start the
MCP server on stdio and print nothing else, because that is the contract every
MCP client launches it under. With arguments it is a normal command line tool
and has to behave like one.

Getting that balance wrong is what made issue #28 so hard to read. Subcommands
were matched by name and everything else fell through to ``mcp.run()``, so
running a subcommand your installed version did not have started a server:

    $ python -m fxhoudinimcp install
    WARNING:fxhoudinimcp.server:Cannot reach Houdini at startup: ...
    WARNING:fxhoudinimcp.server:Tools will attempt to connect on first use.

Nothing installed, nothing said so, and the two warnings point at Houdini, which
was never involved. So unrecognised arguments are now an error, and ``--version``
exists because "which version am I actually running" was the question that
unlocked it: an editable install reports the commit it was created at, not
whatever the working tree has since become.
"""

from __future__ import annotations

# Built-in
import logging
import os
import sys
from collections.abc import Callable


def _run_install(argv: list[str]) -> int:
    # Imported inside the subcommand so that no-argument startup, the hot path
    # for every MCP client, does not pay for the installer.
    from fxhoudinimcp.install import main as install_main

    return install_main(argv)


def _run_uninstall(argv: list[str]) -> int:
    from fxhoudinimcp.uninstall import main as uninstall_main

    return uninstall_main(argv)


def _run_houdini_package(argv: list[str]) -> int:
    from fxhoudinimcp.houdini_package import main as houdini_package_main

    return houdini_package_main(argv)


# The commands this entry point accepts, each with the one-line summary shown in
# the usage text. Kept as one table so a command cannot exist without being
# listed, which is how the missing-subcommand failure stayed invisible.
SUBCOMMANDS: dict[str, tuple[Callable[[list[str]], int], str]] = {
    "install": (
        _run_install,
        "install the Houdini plugin and register an MCP client",
    ),
    "uninstall": (
        _run_uninstall,
        "remove the Houdini plugin file and the client registration",
    ),
    "houdini-package": (
        _run_houdini_package,
        "print or write the Houdini package file",
    ),
}

_HELP_FLAGS = ("-h", "--help")
_VERSION_FLAGS = ("-V", "--version")


def usage() -> str:
    """The usage text, built from the command table so it cannot drift."""
    width = max(len(name) for name in SUBCOMMANDS)
    commands = "\n".join(
        f"    {name.ljust(width)}   {summary}" for name, (_, summary) in SUBCOMMANDS.items()
    )
    return (
        "usage: python -m fxhoudinimcp [<command>] [<args>]\n"
        "\n"
        "With no arguments, runs the MCP server on stdio. That is how an MCP\n"
        "client starts it, and it is the default.\n"
        "\n"
        "Commands:\n"
        f"{commands}\n"
        "\n"
        "Run any command with --help for its own options.\n"
        "\n"
        "Options:\n"
        "    --exposure full|progressive   tool exposure mode (env: FXHOUDINIMCP_EXPOSURE)\n"
        "    -h, --help                    show this message\n"
        "    -V, --version                 show the installed version\n"
    )


def _parse_server_args(argv: list[str]) -> str | None:
    """Parse server-level CLI flags, exiting 2 on unknown commands or bad options."""
    exposure: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in _HELP_FLAGS:
            print(usage(), end="")
            raise SystemExit(0)
        if arg in _VERSION_FLAGS:
            from fxhoudinimcp import __version__

            print(__version__)
            raise SystemExit(0)
        if arg == "--exposure":
            if index + 1 >= len(argv):
                print(
                    "missing value for --exposure (expected 'full' or 'progressive')\n",
                    file=sys.stderr,
                )
                print(usage(), end="", file=sys.stderr)
                raise SystemExit(2)
            exposure = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--exposure="):
            exposure = arg.split("=", 1)[1]
            index += 1
            continue

        # Never fall through to the server. An unrecognised argument is a
        # mistake, and starting a stdio server in response to a mistake looks
        # exactly like a hung install.
        print(f"unknown command: {arg}\n", file=sys.stderr)
        print(usage(), end="", file=sys.stderr)
        raise SystemExit(2)

    if exposure is not None:
        normalized = exposure.strip().lower()
        if normalized not in ("full", "progressive"):
            print(
                f"invalid exposure mode {exposure!r}; expected 'full' or 'progressive'\n",
                file=sys.stderr,
            )
            print(usage(), end="", file=sys.stderr)
            raise SystemExit(2)
        return normalized
    return None


def main() -> None:
    argv = sys.argv[1:]
    cli_exposure: str | None = None

    if argv:
        command, rest = argv[0], argv[1:]

        if command in SUBCOMMANDS:
            runner, _ = SUBCOMMANDS[command]
            raise SystemExit(runner(rest))

        if command in _HELP_FLAGS:
            print(usage(), end="")
            raise SystemExit(0)

        if command in _VERSION_FLAGS:
            from fxhoudinimcp import __version__

            print(__version__)
            raise SystemExit(0)

        cli_exposure = _parse_server_args(argv)

    from fxhoudinimcp.config import exposure_mode

    try:
        resolved_exposure = cli_exposure or exposure_mode()
    except ValueError as exc:
        print(f"{exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))

    # Import server (triggers tool, resource, and prompt registration)
    from fxhoudinimcp import prompts as _prompts  # noqa: F401
    from fxhoudinimcp import resources as _resources  # noqa: F401

    # Force import all tool modules to register them
    from fxhoudinimcp import tools as _tools  # noqa: F401
    from fxhoudinimcp._sdk import run_server
    from fxhoudinimcp.server import configure_exposure, mcp  # noqa: F811

    configure_exposure(resolved_exposure)
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    run_server(mcp, transport=transport)


if __name__ == "__main__":
    main()
