from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

MEETING_APPS: tuple[str, ...] = (
    "zoom",
    "google chrome",
    "chrome",
    "chromium",
    "slack",
    "firefox",
)


@dataclass(frozen=True)
class CapturingApp:
    name: str
    pid: int | None


def list_mic_capturing_apps() -> list[CapturingApp]:
    """Apps currently capturing the mic, per PipeWire node graph.

    A meeting app shows up as a `Stream/Input/Audio` node with an
    `application.name`. Returns the union for the current pw-dump snapshot.
    """
    try:
        raw = subprocess.check_output(["pw-dump"], stderr=subprocess.DEVNULL, timeout=5)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    found: list[CapturingApp] = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {}) or {}
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        name = props.get("application.name")
        if not name:
            continue
        pid_raw = props.get("application.process.id")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid = None
        found.append(CapturingApp(name=name, pid=pid))
    return found


def find_meeting_app(capturing: list[CapturingApp] | None = None) -> CapturingApp | None:
    """Return the first whitelisted meeting app currently capturing the mic."""
    apps = capturing if capturing is not None else list_mic_capturing_apps()
    for app in apps:
        lname = app.name.lower()
        if any(token in lname for token in MEETING_APPS):
            return app
    return None
