from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"


def diarize_audio(audio_path: Path, hf_token: str) -> list[dict[str, Any]]:
    """Diarize the audio with pyannote. Returns turns sorted by start time.

    Each turn: `{"start": float, "end": float, "speaker": str}` where `speaker`
    is a cluster label like "SPEAKER_00", "SPEAKER_01".
    """
    import torch
    import torchaudio
    from pyannote.audio import Pipeline

    # pyannote.audio renamed its auth kwarg (token → use_auth_token → token).
    # Detect the live signature so this stays compatible across versions.
    sig = inspect.signature(Pipeline.from_pretrained)
    auth_kwarg = "token" if "token" in sig.parameters else "use_auth_token"
    pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, **{auth_kwarg: hf_token})
    if pipeline is None:
        raise RuntimeError(
            f"Failed to load {DIARIZATION_MODEL}. Check that HF_TOKEN is valid and "
            f"that you accepted the EULA at https://huggingface.co/{DIARIZATION_MODEL}"
        )
    if torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))

    # Load the waveform into memory and pass it as a dict so pyannote uses its
    # in-memory crop path, which pads short tail chunks with zeros via F.pad.
    # The file-based path in pyannote 4 strictly compares actual vs expected
    # sample counts and raises on Opus mixdowns whose tail chunk falls short.
    waveform, sample_rate = torchaudio.load(str(audio_path))
    diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate})
    # pyannote 4 returns a DiarizeOutput dataclass; 3.x returned an Annotation
    # directly. Prefer exclusive_speaker_diarization (no overlapping turns) since
    # downstream whisper segments are non-overlapping anyway.
    annotation = getattr(diarization, "exclusive_speaker_diarization", diarization)
    turns: list[dict[str, Any]] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)})
    turns.sort(key=lambda t: t["start"])
    return turns
