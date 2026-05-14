from __future__ import annotations

from pathlib import Path

from meetcap.audio.recorder import _build_ffmpeg_cmd
from meetcap.audio.sources import AudioSources


def test_ffmpeg_cmd_shape() -> None:
    sources = AudioSources(mic="mic_x", sink_monitor="monitor_y")
    cmd = _build_ffmpeg_cmd(sources, Path("/tmp/out.flac"))

    # Both inputs pulled from pulse with explicit thread_queue_size and -ac 1.
    assert cmd.count("pulse") == 2
    assert cmd.count("-thread_queue_size") == 2
    assert "-ac" in cmd
    assert cmd.count("-ac") == 2

    # Specific sources appear as input args.
    assert "mic_x" in cmd
    assert "monitor_y" in cmd

    # Filter graph must downmix each input to mono before amerge so channel
    # mapping is unambiguous (L=mic, R=system).
    fg_index = cmd.index("-filter_complex")
    fg = cmd[fg_index + 1]
    assert "aformat=channel_layouts=mono" in fg
    assert "amerge=inputs=2" in fg
    assert "channel_layouts=stereo" in fg

    # Output: FLAC at 16 kHz.
    assert "flac" in cmd
    assert "16000" in cmd
    assert str(Path("/tmp/out.flac")) == cmd[-1]
