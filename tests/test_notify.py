from __future__ import annotations

import json
from pathlib import Path

from meetcap import notify


def test_drive_url_from_state(tmp_path: Path) -> None:
    md = tmp_path / "meeting"
    md.mkdir()
    (md / ".drive-upload.json").write_text(json.dumps({"folder_id": "abc123"}))
    assert notify.drive_url_from_state(md) == "https://drive.google.com/drive/folders/abc123"


def test_drive_url_returns_none_without_state(tmp_path: Path) -> None:
    md = tmp_path / "meeting"
    md.mkdir()
    assert notify.drive_url_from_state(md) is None


def test_drive_url_returns_none_on_bad_json(tmp_path: Path) -> None:
    md = tmp_path / "meeting"
    md.mkdir()
    (md / ".drive-upload.json").write_text("{not json")
    assert notify.drive_url_from_state(md) is None


def test_notify_no_op_when_notify_send_missing(monkeypatch) -> None:
    monkeypatch.setattr(notify.shutil, "which", lambda _name: None)
    # Must not raise even if Popen would otherwise be called.
    notify.notify("title", "body")
