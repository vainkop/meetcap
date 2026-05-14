from __future__ import annotations

from pathlib import Path

import pytest

from meetcap.transcribe import mixdown


def test_mixdown_invokes_ffmpeg_with_correct_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    flac = tmp_path / "audio.flac"
    flac.write_bytes(b"fake")
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **_kw):
        captured["cmd"] = list(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(mixdown.subprocess, "run", _fake_run)

    out = mixdown.make_opus_mixdown(flac)
    assert out.name == "audio.mono.ogg"  # .ogg, not .opus — OpenAI rejects bare .opus

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "libopus" in cmd
    assert "24k" in cmd
    assert "16000" in cmd
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"


def test_mixdown_idempotent_skip_when_newer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    flac = tmp_path / "audio.flac"
    flac.write_bytes(b"fake")
    ogg = tmp_path / "audio.mono.ogg"
    ogg.write_bytes(b"existing")
    import os
    import time

    # Make the .ogg newer than the .flac.
    later = time.time() + 60
    os.utime(ogg, (later, later))

    called = {"n": 0}

    def _fake_run(*_a, **_kw):
        called["n"] += 1
        raise AssertionError("should not invoke ffmpeg when output is newer")

    monkeypatch.setattr(mixdown.subprocess, "run", _fake_run)

    out = mixdown.make_opus_mixdown(flac)
    assert out == ogg
    assert called["n"] == 0
