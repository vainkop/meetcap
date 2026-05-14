from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

EMBEDDING_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
COSINE_THRESHOLD = 0.30  # below this, treat cluster as unenrolled


def _get_classifier() -> Any:
    """Load SpeechBrain's ECAPA-TDNN speaker embedding model."""
    import torch
    from speechbrain.inference.speaker import EncoderClassifier

    device = "cuda" if torch.cuda.is_available() else "cpu"
    return EncoderClassifier.from_hparams(
        source=EMBEDDING_MODEL,
        savedir=str(Path.home() / ".cache" / "speechbrain" / "ecapa-voxceleb"),
        run_opts={"device": device},
    )


def _embedding_from_clip(classifier: Any, clip_path: Path) -> Any:
    """Get a 192-d embedding (numpy array) for a short audio clip."""
    emb = classifier.encode_batch(_load_waveform(clip_path))
    return emb.squeeze().detach().cpu().numpy()


def _load_waveform(path: Path) -> Any:
    """Return a (1, n_samples) tensor at 16 kHz for the audio file."""
    import torchaudio

    wav, sr = torchaudio.load(str(path))
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
    return wav


def _longest_segment_per_cluster(turns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each diarized cluster, pick its single longest turn as the representative span."""
    best: dict[str, dict[str, Any]] = {}
    for turn in turns:
        cluster = str(turn["speaker"])
        length = turn["end"] - turn["start"]
        if cluster not in best or length > (best[cluster]["end"] - best[cluster]["start"]):
            best[cluster] = turn
    return best


def _extract_clip(audio_path: Path, start: float, end: float, dest: Path) -> None:
    """Cut [start, end] from audio_path into dest WAV via ffmpeg."""
    import subprocess

    duration = max(0.5, end - start)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(dest),
        ],
        check=True,
    )


def _cosine(a: Any, b: Any) -> float:
    import numpy as np

    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def match_clusters_to_enrolled(
    audio_path: Path,
    diarization_turns: list[dict[str, Any]],
    enrolled: list[tuple[str, Path]],
) -> dict[str, str]:
    """Return a mapping {cluster_label -> enrolled_name} for clusters that pass the
    cosine-similarity threshold. Unmatched clusters keep their original labels.
    """
    if not enrolled or not diarization_turns:
        return {}

    classifier = _get_classifier()

    enrolled_emb: dict[str, Any] = {}
    for name, path in enrolled:
        enrolled_emb[name] = _embedding_from_clip(classifier, path)

    best_segments = _longest_segment_per_cluster(diarization_turns)
    mapping: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        for cluster, turn in best_segments.items():
            clip = Path(td) / f"{cluster}.wav"
            _extract_clip(audio_path, turn["start"], turn["end"], clip)
            cluster_emb = _embedding_from_clip(classifier, clip)
            best_name = None
            best_score = -1.0
            for name, ne in enrolled_emb.items():
                score = _cosine(cluster_emb, ne)
                if score > best_score:
                    best_score = score
                    best_name = name
            if best_name is not None and best_score >= COSINE_THRESHOLD:
                mapping[cluster] = best_name
    return mapping
