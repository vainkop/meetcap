from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console

from meetcap.config import Settings
from meetcap.drive import auth, client

STATE_FILENAME = ".drive-upload.json"

# Order matters for the upload summary; transcript.md uploaded twice (raw .md +
# Google Doc conversion) so it's both searchable in Drive and downloadable.
_UPLOAD_PLAN: list[tuple[str, str, bool, str]] = [
    ("audio.flac", "audio/flac", False, "audio.flac"),
    ("transcript.json", "application/json", False, "transcript.json"),
    ("transcript.md", "text/markdown", False, "transcript.md"),
    ("metadata.json", "application/json", False, "metadata.json"),
    ("transcript.md", "text/markdown", True, "transcript"),  # Google Doc copy
]


def _load_state(meeting_dir: Path) -> dict[str, Any]:
    p = meeting_dir / STATE_FILENAME
    if not p.exists():
        return {}
    try:
        return dict(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(meeting_dir: Path, state: dict[str, Any]) -> None:
    p = meeting_dir / STATE_FILENAME
    p.write_text(json.dumps(state, indent=2))


def upload_meeting(meeting_dir: Path, settings: Settings, console: Console) -> str:
    """Idempotently mirror a meeting folder to Google Drive. Returns the folder URL."""
    creds = auth.load_credentials()
    service = client.build_drive(creds)
    state = _load_state(meeting_dir)

    folder_id: str | None = state.get("folder_id")
    if not folder_id:
        folder_id = client.create_folder(service, meeting_dir.name, settings.drive.parent_folder_id)
        state["folder_id"] = folder_id
        _save_state(meeting_dir, state)
        console.print(f"[green]created folder[/green] {meeting_dir.name} ({folder_id})")
    else:
        console.print(f"[dim]reusing folder[/dim] {folder_id}")

    files_uploaded: dict[str, str] = dict(state.get("files", {}))

    for src_name, mimetype, convert, key in _UPLOAD_PLAN:
        if convert and not settings.drive.convert_markdown_to_doc:
            continue
        src = meeting_dir / src_name
        if not src.is_file():
            continue
        if key in files_uploaded:
            console.print(f"[dim]skip[/dim] {key} (already uploaded)")
            continue
        file_id = client.upload_file(
            service,
            src,
            folder_id,
            mimetype=mimetype,
            target_name=key if convert else None,
            convert_to_doc=convert,
        )
        files_uploaded[key] = file_id
        label = f"{key} (Google Doc)" if convert else key
        console.print(f"[green]uploaded[/green] {label} → {file_id}")

    state["files"] = files_uploaded
    _save_state(meeting_dir, state)
    return client.folder_web_url(folder_id)
