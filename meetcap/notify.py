from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

NOTIFY_ICON = Path(__file__).parent / "assets" / "meetcap-idle.svg"


def notify(title: str, body: str, urgency: str = "normal") -> None:
    """Show a desktop notification via libnotify. No-op if notify-send is missing."""
    if not shutil.which("notify-send"):
        return
    cmd = [
        "notify-send",
        "-a",
        "meetcap",
        "-i",
        str(NOTIFY_ICON),
        "-u",
        urgency,
        title,
        body,
    ]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def drive_url_from_state(meeting_dir: Path) -> str | None:
    state = meeting_dir / ".drive-upload.json"
    if not state.is_file():
        return None
    try:
        folder_id = json.loads(state.read_text()).get("folder_id")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(folder_id, str):
        return None
    return f"https://drive.google.com/drive/folders/{folder_id}"


def notify_post_process_success(meeting_dir: Path) -> None:
    drive_url = drive_url_from_state(meeting_dir)
    body = drive_url or "transcribed locally"
    notify(f"✓ {meeting_dir.name}", body)


def notify_post_process_failure(meeting_dir: Path, log_path: Path) -> None:
    notify(f"✗ {meeting_dir.name} failed", f"See {log_path}", urgency="critical")


def notify_recording_died(meeting_dir: Path, exit_code: int) -> None:
    """ffmpeg exited on its own mid-capture — alert the user immediately so
    they don't keep speaking into a dead recorder."""
    notify(
        f"⚠ recording stopped: {meeting_dir.name}",
        f"ffmpeg exited (rc={exit_code}); will resume if the meeting app keeps capturing.",
        urgency="critical",
    )
