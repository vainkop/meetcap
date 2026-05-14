from __future__ import annotations

import os
import subprocess


def active_window_title() -> str | None:
    """Return the currently-focused X11 window title, or None if unavailable.

    Used purely to seed the meeting slug. Returns None on Wayland / no DISPLAY /
    xdotool failure — callers must fall back to a default.
    """
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
        return None
    if not os.environ.get("DISPLAY"):
        return None
    try:
        out = subprocess.check_output(
            ["xdotool", "getactivewindow", "getwindowname"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    title = out.decode("utf-8", errors="replace").strip()
    return title or None
