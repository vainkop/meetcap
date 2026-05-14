from __future__ import annotations

import subprocess
from pathlib import Path


def make_opus_mixdown(source_flac: Path, output_opus: Path | None = None) -> Path:
    """Produce a mono 16 kHz libopus 24 kbps mixdown for cloud transcription.

    The 25 MB cap on /v1/audio/transcriptions makes Opus 24 kbps a sweet spot:
    ~3 MB per hour, no quality drop relative to FLAC for the diarize model.
    Idempotent: skips re-encoding if the output is newer than the source.

    Note: filename uses `.ogg` (Ogg Opus container) — OpenAI's API rejects the
    bare `.opus` extension, even though it accepts the Opus codec inside .ogg.
    """
    out = output_opus or source_flac.with_name("audio.mono.ogg")
    if out.exists() and out.stat().st_mtime >= source_flac.stat().st_mtime:
        return out
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(source_flac),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            "-y",
            str(out),
        ],
        check=True,
    )
    return out
