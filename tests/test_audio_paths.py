from __future__ import annotations

from datetime import datetime

from meetcap.audio import paths as audio_paths


def test_build_meeting_dir_format(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_paths, "RECORDINGS_ROOT", tmp_path)
    when = datetime(2026, 5, 13, 14, 30)
    out = audio_paths.build_meeting_dir("Kickoff Call!", when=when)
    assert out.parent == tmp_path
    assert out.name == "2026-05-13_1430_kickoff-call"
    assert out.is_dir()


def test_build_meeting_dir_slugify_collapses_punctuation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_paths, "RECORDINGS_ROOT", tmp_path)
    when = datetime(2026, 5, 13, 14, 30)
    out = audio_paths.build_meeting_dir("Zoom // Workplace --- Daily / Sync", when=when)
    assert "zoom-workplace-daily-sync" in out.name


def test_build_meeting_dir_empty_slug_falls_back(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_paths, "RECORDINGS_ROOT", tmp_path)
    when = datetime(2026, 5, 13, 14, 30)
    out = audio_paths.build_meeting_dir("@@@", when=when)
    assert out.name.endswith("_untitled")
