from __future__ import annotations

from pathlib import Path
from typing import Any

from meetcap.config import Settings
from meetcap.voice.enroll import list_enrolled_speakers


def _assign_speaker(seg_start: float, seg_end: float, turns: list[dict[str, Any]]) -> str:
    """Pick the diarized cluster with the most overlap with the given segment."""
    best_label = "?"
    best_overlap = 0.0
    for turn in turns:
        overlap = max(0.0, min(seg_end, turn["end"]) - max(seg_start, turn["start"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = turn["speaker"]
    return best_label


def _renumber_clusters(segments: list[dict[str, Any]], mapping: dict[str, str]) -> None:
    """Re-label segments: enrolled clusters get their name; remaining clusters
    are renumbered as 'A', 'B', 'C', ... in first-appearance order to mirror
    the cloud diarize convention."""
    seen: list[str] = []
    for seg in segments:
        original = seg["speaker"]
        if original in mapping:
            seg["speaker"] = mapping[original]
            continue
        if original not in seen:
            seen.append(original)
        # Map SPEAKER_00 -> 'A', SPEAKER_01 -> 'B', ...
        idx = seen.index(original)
        if idx < 26:
            seg["speaker"] = chr(ord("A") + idx)


def transcribe_local(audio_path: Path, settings: Settings) -> dict[str, Any]:
    """Local-backend equivalent of `transcribe_diarized`. Returns a dict shaped
    like the OpenAI diarize response (segments + duration + text + usage).
    """
    from meetcap.transcribe.local_diarize import diarize_audio
    from meetcap.transcribe.local_whisper import transcribe_audio
    from meetcap.transcribe.speaker_match import match_clusters_to_enrolled

    if not settings.hf_token:
        raise RuntimeError("HF_TOKEN not set; pyannote diarization requires it")

    whisper_segments, duration, language = transcribe_audio(
        audio_path,
        model_name=settings.transcription.local_model,
        compute_type=settings.transcription.local_compute_type,
        language=settings.transcription.language,
    )
    turns = diarize_audio(audio_path, hf_token=settings.hf_token)
    enrolled = list_enrolled_speakers()
    mapping = match_clusters_to_enrolled(audio_path, turns, enrolled)

    segments: list[dict[str, Any]] = []
    for i, seg in enumerate(whisper_segments):
        cluster = _assign_speaker(seg["start"], seg["end"], turns)
        segments.append(
            {
                "type": "transcript.text.segment",
                "id": f"seg_{i:04d}",
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "speaker": cluster,
            }
        )
    _renumber_clusters(segments, mapping)

    return {
        "task": "transcribe",
        "duration": duration,
        "language": language,
        "text": "\n".join(s["text"] for s in segments),
        "segments": segments,
        # No `usage` field — local is free.
    }
