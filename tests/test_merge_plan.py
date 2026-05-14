from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from meetcap.merge.plan import (
    ACTIVE_RECORDING_THRESHOLD_SECONDS,
    MergeError,
    MergePlan,
)
from meetcap.merge.splice import MERGED_FROM_FILE, write_merge_markers
from meetcap.tray.retry import MERGED_INTO_FILE, RetryQueue


def _make_meeting(tmp_path: Path, name: str, mtime_offset: float = -300.0) -> Path:
    """Create <tmp_path>/<name>/audio.flac with an old mtime so it doesn't look
    like a live recording. mtime_offset is seconds before now (negative = past)."""
    d = tmp_path / name
    d.mkdir()
    audio = d / "audio.flac"
    audio.write_bytes(b"\x00" * 1024)
    ts = time.time() + mtime_offset
    os.utime(audio, (ts, ts))
    return d


def test_build_orders_chronologically(tmp_path: Path) -> None:
    """Inputs out of order get re-sorted by the timestamp in the dir name."""
    b = _make_meeting(tmp_path, "2026-05-14_1305_b")
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    c = _make_meeting(tmp_path, "2026-05-14_1400_c")
    plan = MergePlan.build([c, a, b])
    assert [s.dir for s in plan.sources] == [a, b, c]
    # Output dir uses earliest start's timestamp.
    assert plan.output_dir.name.startswith("2026-05-14_1200_")
    assert plan.output_dir.name.endswith("-merged")


def test_build_rejects_single_source(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    with pytest.raises(MergeError, match="at least two"):
        MergePlan.build([a])


def test_build_rejects_missing_audio(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    b = tmp_path / "2026-05-14_1305_b"
    b.mkdir()  # no audio.flac
    with pytest.raises(MergeError, match="no audio.flac"):
        MergePlan.build([a, b])


def test_build_rejects_active_recording(tmp_path: Path) -> None:
    """audio.flac written within the last 30 s = live ffmpeg writing to it."""
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    b = _make_meeting(
        tmp_path, "2026-05-14_1305_b", mtime_offset=-(ACTIVE_RECORDING_THRESHOLD_SECONDS - 5)
    )
    with pytest.raises(MergeError, match="actively recording"):
        MergePlan.build([a, b])


def test_build_rejects_already_merged_source(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    b = _make_meeting(tmp_path, "2026-05-14_1305_b")
    (a / MERGED_INTO_FILE).write_text("{}")
    with pytest.raises(MergeError, match="already merged"):
        MergePlan.build([a, b])


def test_build_rejects_duplicate_inputs(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    with pytest.raises(MergeError, match="duplicate"):
        MergePlan.build([a, a])


def test_build_rejects_unparseable_dir(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    bad = tmp_path / "no-timestamp-here"
    bad.mkdir()
    (bad / "audio.flac").write_bytes(b"\x00")
    os.utime(bad / "audio.flac", (time.time() - 300, time.time() - 300))
    with pytest.raises(MergeError, match="unparseable"):
        MergePlan.build([a, bad])


def test_build_uses_custom_slug(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    b = _make_meeting(tmp_path, "2026-05-14_1305_b")
    plan = MergePlan.build([a, b], custom_slug="q1-review")
    assert plan.output_dir.name == "2026-05-14_1200_q1-review"


def test_build_avoids_overwriting_source_dir(tmp_path: Path) -> None:
    """If a custom slug + first timestamp would land on a source dir, bump it."""
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    b = _make_meeting(tmp_path, "2026-05-14_1305_b")
    plan = MergePlan.build([a, b], custom_slug="a")
    # Would collide with `a` itself — must bump.
    assert plan.output_dir != a
    assert plan.output_dir.name == "2026-05-14_1200_a-merged"


def test_write_merge_markers(tmp_path: Path) -> None:
    a = _make_meeting(tmp_path, "2026-05-14_1200_a")
    b = _make_meeting(tmp_path, "2026-05-14_1305_b")
    plan = MergePlan.build([a, b])
    write_merge_markers(plan)
    # Each source gets .merged-into pointing at the output dir basename.
    for src in (a, b):
        marker = json.loads((src / MERGED_INTO_FILE).read_text())
        assert marker["merged_dir"] == plan.output_dir.name
    # Output dir gets .merged-from listing the source basenames in order.
    out = json.loads((plan.output_dir / MERGED_FROM_FILE).read_text())
    assert out["sources"] == [a.name, b.name]


def test_retry_queue_scan_skips_merged_into(tmp_path: Path) -> None:
    """A source with .merged-into.json must NOT be queued by scan() — its
    content has already been folded into the merged meeting."""
    meeting = _make_meeting(tmp_path, "2026-05-14_1200_a")
    (meeting / ".transcribe-error.log").write_text("APIConnectionError\n")
    (meeting / MERGED_INTO_FILE).write_text(json.dumps({"merged_dir": "x"}))
    q = RetryQueue()
    q.scan(tmp_path)
    assert q.pending() == []


def test_retry_queue_scan_drops_stale_retry_state_on_merged_into(tmp_path: Path) -> None:
    """If the source had a .retry-state.json from before the merge, scan()
    should clean it up so the queue doesn't keep retrying it post-merge."""
    meeting = _make_meeting(tmp_path, "2026-05-14_1200_a")
    (meeting / ".retry-state.json").write_text(
        json.dumps(
            {
                "attempts": 1,
                "next_attempt_at": time.time(),
                "last_error": "boom",
                "stage": "transcribe",
            }
        )
    )
    (meeting / MERGED_INTO_FILE).write_text(json.dumps({"merged_dir": "x"}))
    q = RetryQueue()
    q.scan(tmp_path)
    assert q.pending() == []
    # The stale retry-state file is removed too.
    assert not (meeting / ".retry-state.json").exists()


def test_retry_queue_add_skips_merged_into(tmp_path: Path) -> None:
    """If a merge happened between an in-flight retry being kicked off and
    its failure landing in _record_failure, add() must drop the failure
    rather than re-queuing the now-merged source."""
    meeting = tmp_path / "2026-05-14_1200_a"
    meeting.mkdir()
    (meeting / MERGED_INTO_FILE).write_text(json.dumps({"merged_dir": "x"}))
    q = RetryQueue()
    q.add(meeting, "boom", "transcribe")
    assert q.pending() == []
    assert not (meeting / ".retry-state.json").exists()
