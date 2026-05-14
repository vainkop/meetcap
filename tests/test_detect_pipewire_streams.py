from __future__ import annotations

import json

import pytest

from meetcap.detect import pipewire_streams as pw


def _make_node(app_name: str, media_class: str = "Stream/Input/Audio", pid: int = 1234) -> dict:
    return {
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {
                "media.class": media_class,
                "application.name": app_name,
                "application.process.id": pid,
            }
        },
    }


def _patch_pw_dump(monkeypatch: pytest.MonkeyPatch, payload: list[dict]) -> None:
    monkeypatch.setattr(
        pw.subprocess, "check_output", lambda *a, **kw: json.dumps(payload).encode()
    )


def test_find_meeting_app_matches_zoom(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pw_dump(monkeypatch, [_make_node("ZOOM VoiceEngine", pid=42)])
    app = pw.find_meeting_app()
    assert app is not None
    assert "zoom" in app.name.lower()
    assert app.pid == 42


def test_find_meeting_app_matches_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pw_dump(monkeypatch, [_make_node("Google Chrome")])
    app = pw.find_meeting_app()
    assert app is not None
    assert "chrome" in app.name.lower()


def test_find_meeting_app_skips_non_input_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pw_dump(
        monkeypatch,
        [_make_node("Zoom", media_class="Stream/Output/Audio")],
    )
    assert pw.find_meeting_app() is None


def test_find_meeting_app_no_match_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pw_dump(monkeypatch, [_make_node("Some Random App")])
    assert pw.find_meeting_app() is None


def test_find_meeting_app_pw_dump_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_kw):
        raise FileNotFoundError

    monkeypatch.setattr(pw.subprocess, "check_output", _boom)
    assert pw.find_meeting_app() is None
