from __future__ import annotations

import json

import pytest

from meetcap.audio import sources
from meetcap.config import AudioConfig


def _fake_pw_dump_payload() -> bytes:
    return json.dumps(
        [
            {
                "type": "PipeWire:Interface:Metadata",
                "props": {"metadata.name": "default"},
                "metadata": [
                    {
                        "key": "default.audio.sink",
                        "value": {"name": "alsa_output.x__sink"},
                    },
                    {
                        "key": "default.audio.source",
                        "value": {"name": "alsa_input.x__source"},
                    },
                ],
            }
        ]
    ).encode()


def test_resolve_uses_pw_dump_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sources.subprocess, "check_output", lambda *a, **kw: _fake_pw_dump_payload()
    )
    result = sources.resolve(AudioConfig())
    assert result.mic == "alsa_input.x__source"
    assert result.sink_monitor == "alsa_output.x__sink.monitor"


def test_config_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sources.subprocess, "check_output", lambda *a, **kw: _fake_pw_dump_payload()
    )
    cfg = AudioConfig(mic_source="mic.override", sink_monitor="sink.override.monitor")
    result = sources.resolve(cfg)
    assert result.mic == "mic.override"
    assert result.sink_monitor == "sink.override.monitor"


def test_pw_dump_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_kw):
        raise FileNotFoundError("no pw-dump")

    monkeypatch.setattr(sources.subprocess, "check_output", boom)
    with pytest.raises(sources.SourceResolutionError):
        sources.resolve(AudioConfig())


def test_default_metadata_absent_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources.subprocess, "check_output", lambda *a, **kw: b"[]")
    with pytest.raises(sources.SourceResolutionError):
        sources.resolve(AudioConfig())
