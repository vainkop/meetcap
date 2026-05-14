from __future__ import annotations

import json
from pathlib import Path

from meetcap.drive import upload as drive_upload


def test_upload_plan_includes_doc_conversion() -> None:
    """transcript.md must appear twice: once raw, once converted to Google Doc."""
    plan = drive_upload._UPLOAD_PLAN
    md_entries = [row for row in plan if row[0] == "transcript.md"]
    assert len(md_entries) == 2
    raw = next(row for row in md_entries if row[2] is False)
    doc = next(row for row in md_entries if row[2] is True)
    assert raw[1] == "text/markdown"
    assert doc[1] == "text/markdown"
    assert doc[3] == "transcript"  # named with no extension when converted


def test_state_round_trip(tmp_path: Path) -> None:
    meeting_dir = tmp_path / "2026-05-13_meeting"
    meeting_dir.mkdir()

    assert drive_upload._load_state(meeting_dir) == {}

    drive_upload._save_state(meeting_dir, {"folder_id": "FID", "files": {"a": "X"}})
    state = drive_upload._load_state(meeting_dir)
    assert state["folder_id"] == "FID"
    assert state["files"] == {"a": "X"}

    on_disk = json.loads((meeting_dir / ".drive-upload.json").read_text())
    assert on_disk == state


def test_load_state_tolerates_corrupt_json(tmp_path: Path) -> None:
    meeting_dir = tmp_path / "x"
    meeting_dir.mkdir()
    (meeting_dir / ".drive-upload.json").write_text("{ not json")
    assert drive_upload._load_state(meeting_dir) == {}
