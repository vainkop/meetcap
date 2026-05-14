from __future__ import annotations

from pathlib import Path

import pytest

from meetcap import config


def test_load_settings_defaults_from_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no user config.toml exists, fall back to config.toml.example bundled with the repo."""
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "config.toml")
    # The example file is in the actual repo; keep that path so we test the
    # real default-population code path.
    s = config.load_settings()
    assert s.transcription.model == "gpt-4o-transcribe-diarize"
    assert s.drive.parent_folder_id == "10RTchUbN_ESFsgfA5tlTZxyF-yBbXM6r"
    assert s.audio.mic_boost_to == 2.0
    assert s.transcription.summary_enabled is True
    assert s.notifications.enabled is True


def test_user_config_overrides_example(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    user_cfg = tmp_path / "config.toml"
    user_cfg.write_text(
        "[audio]\nmic_boost_to = 1.25\n"
        "[transcription]\nsummary_enabled = false\n"
        "[notifications]\nenabled = false\n"
    )
    monkeypatch.setattr(config, "USER_CONFIG_PATH", user_cfg)
    s = config.load_settings()
    assert s.audio.mic_boost_to == 1.25
    assert s.transcription.summary_enabled is False
    assert s.notifications.enabled is False
    # Fields not overridden inherit defaults.
    assert s.drive.parent_folder_id == "10RTchUbN_ESFsgfA5tlTZxyF-yBbXM6r"
