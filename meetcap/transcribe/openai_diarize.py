from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from openai import OpenAI

MAX_KNOWN_SPEAKERS = 4


def _data_url_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime is None:
        mime = "audio/wav"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def transcribe_diarized(
    client: OpenAI,
    audio_path: Path,
    model: str,
    speaker_samples: list[tuple[str, Path]],
) -> Any:
    """Call /v1/audio/transcriptions with diarization.

    speaker_samples: up to 4 (name, path) pairs of enrolled voice samples;
    pass [] for unenrolled diarization (segments come back as A, B, C, ...).
    """
    extra_body: dict[str, list[str]] = {}
    if speaker_samples:
        usable = speaker_samples[:MAX_KNOWN_SPEAKERS]
        extra_body["known_speaker_names"] = [name for name, _ in usable]
        extra_body["known_speaker_references"] = [_data_url_for(p) for _, p in usable]

    with audio_path.open("rb") as fh:
        return client.audio.transcriptions.create(
            model=model,
            file=fh,
            response_format="diarized_json",
            chunking_strategy="auto",
            extra_body=extra_body,
        )
