from __future__ import annotations

import json
from pathlib import Path

from meetcap.transcribe.render import _fmt_ts, render_all


def test_fmt_ts_hms() -> None:
    assert _fmt_ts(0) == "00:00:00"
    assert _fmt_ts(65) == "00:01:05"
    assert _fmt_ts(3725) == "01:02:05"


def _fake_payload() -> dict:
    return {
        "task": "transcribe",
        "duration": 12.5,
        "text": "Hi.\nHello there.",
        "segments": [
            {
                "type": "transcript.text.segment",
                "id": "s1",
                "start": 0.0,
                "end": 2.0,
                "speaker": "bob",
                "text": "Hi.",
            },
            {
                "type": "transcript.text.segment",
                "id": "s2",
                "start": 2.5,
                "end": 6.0,
                "speaker": "A",
                "text": "Hello there.",
            },
            {
                "type": "transcript.text.segment",
                "id": "s3",
                "start": 6.0,
                "end": 9.0,
                "speaker": "bob",
                "text": "Bye.",
            },
        ],
        "usage": {"type": "duration", "seconds": 13},
    }


def test_render_all_writes_three_files(tmp_path: Path) -> None:
    payload = _fake_payload()
    js, md, meta = render_all(
        payload,
        meeting_dir=tmp_path,
        model="gpt-4o-transcribe-diarize",
        started_iso="2026-05-13T17:29:00+00:00",
        cost_per_minute_usd=0.006,
    )
    assert js.name == "transcript.json"
    assert md.name == "transcript.md"
    assert meta.name == "metadata.json"

    parsed = json.loads(js.read_text())
    assert parsed["model"] == "gpt-4o-transcribe-diarize"
    assert parsed["duration"] == 12.5
    assert len(parsed["segments"]) == 3

    md_text = md.read_text()
    assert "## [00:00:00] bob" in md_text
    assert "## [00:00:02] A" in md_text
    assert "## [00:00:06] bob" in md_text  # speaker switches back -> new header
    assert "Hi." in md_text
    assert "Bye." in md_text

    meta_doc = json.loads(meta.read_text())
    assert meta_doc["speaker_count"] == 2
    assert sorted(meta_doc["speakers"]) == ["A", "bob"]
    assert meta_doc["cost_estimate_usd"] == round(12.5 / 60 * 0.006, 4)


def test_render_metadata_cost_none_when_rate_missing(tmp_path: Path) -> None:
    _, _, meta = render_all(
        _fake_payload(),
        meeting_dir=tmp_path,
        model="x",
        started_iso="2026-05-13T17:29:00+00:00",
        cost_per_minute_usd=None,
    )
    assert json.loads(meta.read_text())["cost_estimate_usd"] is None
