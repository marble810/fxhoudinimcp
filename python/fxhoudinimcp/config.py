"""Runtime configuration flags read from environment variables."""

from __future__ import annotations

# Built-in
import os

_FALSY = {"0", "false", "off", "no"}
EXPOSURE_MODES = ("full", "progressive")


def auto_layout_enabled() -> bool:
    """Whether tools may auto-arrange nodes in the network editor.

    Controlled by the ``FXHOUDINIMCP_AUTO_LAYOUT`` environment variable.
    Off by default so existing node layouts are never rearranged; set to
    ``1`` to opt in.
    """
    value = os.getenv("FXHOUDINIMCP_AUTO_LAYOUT", "0")
    return value.strip().lower() not in _FALSY


def exposure_mode() -> str:
    """Which tool surface the MCP server exposes (``full`` or ``progressive``).

    Controlled by ``FXHOUDINIMCP_EXPOSURE`` (default ``"full"``). CLI
    ``--exposure`` takes precedence over this environment variable.
    """
    value = os.getenv("FXHOUDINIMCP_EXPOSURE", "full").strip().lower()
    if not value:
        return "full"
    if value not in EXPOSURE_MODES:
        expected = ", ".join(EXPOSURE_MODES)
        raise ValueError(f"Invalid FXHOUDINIMCP_EXPOSURE {value!r}; expected one of: {expected}.")
    return value
