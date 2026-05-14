from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _as_dict(obj: Any) -> dict[str, Any]:
    """Normalize the diarize response into a plain dict (SDK returns a pydantic-like object)."""
    if hasattr(obj, "model_dump"):
        return cast(dict[str, Any], obj.model_dump())
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return cast(dict[str, Any], obj.to_dict())
    return dict(obj)


def _segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("segments") or []
    out: list[dict[str, Any]] = []
    for seg in raw:
        if hasattr(seg, "model_dump"):
            seg = seg.model_dump()
        out.append(dict(seg))
    return out


def write_transcript_json(payload: dict[str, Any], path: Path, model: str) -> None:
    out: dict[str, Any] = {
        "model": model,
        "duration": payload.get("duration"),
        "text": payload.get("text"),
        "segments": _segments(payload),
    }
    if "usage" in payload:
        out["usage"] = payload["usage"]
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))


def write_transcript_md(payload: dict[str, Any], path: Path) -> None:
    segments = _segments(payload)
    lines: list[str] = []
    current_speaker: str | None = None
    for seg in segments:
        speaker = seg.get("speaker") or "?"
        start = seg.get("start") or 0.0
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if speaker != current_speaker:
            if lines:
                lines.append("")
            lines.append(f"## [{_fmt_ts(start)}] {speaker}")
            current_speaker = speaker
        lines.append(text)
    path.write_text("\n".join(lines) + "\n")


def write_metadata_json(
    payload: dict[str, Any],
    path: Path,
    model: str,
    started_iso: str,
    cost_per_minute_usd: float | None,
) -> None:
    duration = float(payload.get("duration") or 0.0)
    ended_iso: datetime | None
    if started_iso and duration:
        ended_iso = datetime.fromisoformat(started_iso) + timedelta(seconds=duration)
    elif started_iso:
        ended_iso = datetime.fromisoformat(started_iso)
    else:
        ended_iso = datetime.now(UTC)

    segments = _segments(payload)
    speakers: list[str] = sorted({str(s["speaker"]) for s in segments if s.get("speaker")})

    cost_estimate: float | None = None
    if cost_per_minute_usd is not None and duration:
        cost_estimate = round(duration / 60.0 * cost_per_minute_usd, 4)

    out: dict[str, Any] = {
        "start_iso": started_iso,
        "end_iso": ended_iso.isoformat() if ended_iso else None,
        "duration_sec": round(duration, 2),
        "model": model,
        "speaker_count": len(speakers),
        "speakers": speakers,
        "cost_estimate_usd": cost_estimate,
    }
    if "usage" in payload:
        out["usage"] = payload["usage"]
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))


def render_all(
    payload_obj: Any,
    meeting_dir: Path,
    model: str,
    started_iso: str,
    cost_per_minute_usd: float | None,
) -> tuple[Path, Path, Path]:
    """Render transcript.json / transcript.md / metadata.json. Returns their paths."""
    payload = _as_dict(payload_obj)
    json_path = meeting_dir / "transcript.json"
    md_path = meeting_dir / "transcript.md"
    meta_path = meeting_dir / "metadata.json"
    write_transcript_json(payload, json_path, model)
    write_transcript_md(payload, md_path)
    write_metadata_json(payload, meta_path, model, started_iso, cost_per_minute_usd)
    return json_path, md_path, meta_path
