from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any


# Substrings that mean "ctranslate2's CUDA path is broken on this machine"
# (mismatched cuBLAS, driver too old, missing cuDNN, …). When we see any of
# these in a RuntimeError we transparently retry the same audio on CPU.
# ctranslate2.get_cuda_device_count() is *not* a reliable pre-flight check —
# it returns >0 even when the actual cuBLAS/driver combo fails to load.
def _looks_like_cuda_failure(message: str) -> bool:
    """Any error mentioning CUDA-ish words. Broad on purpose — when we land
    here we already raised RuntimeError, and at that point a CPU retry is
    strictly better than dying with a cryptic GPU error."""
    lowered = message.lower()
    return (
        "cuda" in lowered
        or "libcublas" in lowered
        or "libcudnn" in lowered
        or "out of memory" in lowered
        or "is too old" in lowered
        or "gpu" in lowered
    )


def _collect(segments_iter: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append({"start": float(seg.start), "end": float(seg.end), "text": text})
    return out


def transcribe_audio(
    audio_path: Path,
    model_name: str = "large-v3",
    compute_type: str = "float16",
    language: str | None = None,
) -> tuple[list[dict[str, Any]], float, str]:
    """Run faster-whisper on the audio file.

    Tries GPU first (`device=cuda` with the requested `compute_type`); on any
    CUDA-related RuntimeError — mismatched cuBLAS, driver too old, OOM — falls
    back transparently to CPU (`device=cpu compute_type=int8`). CPU is slower
    on `large-v3` but works on any machine that has the python packages.

    Returns:
        (segments, duration_sec, detected_language)
        where each segment is `{"start": float, "end": float, "text": str}`.
    """
    from faster_whisper import WhisperModel

    def _run(device: str, effective_compute: str) -> tuple[list[dict[str, Any]], Any]:
        model = WhisperModel(model_name, device=device, compute_type=effective_compute)
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=True,
        )
        # Materialize the generator inside the try so a lazy CUDA error
        # surfaces here and we can fall back to CPU.
        return _collect(segments_iter), info

    try:
        segments, info = _run("cuda", compute_type)
    except RuntimeError as e:
        if not _looks_like_cuda_failure(str(e)):
            raise
        print(
            f"[local-whisper] GPU path failed ({type(e).__name__}: {e}); "
            "retrying on CPU (device=cpu compute_type=int8)"
        )
        segments, info = _run("cpu", "int8")
    return segments, float(info.duration), str(info.language)
