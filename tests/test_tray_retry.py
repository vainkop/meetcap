from __future__ import annotations

import json
import time
from pathlib import Path

from meetcap.tray.retry import (
    DELAY_CYCLE,
    MAX_ATTEMPTS_BEFORE_ABANDON,
    PERMANENT_ERROR_PATTERNS,
    RetryQueue,
    delay_for_attempt,
    detect_failed_stage,
    is_permanent_error,
    tail_error,
)


def test_delay_cycle_loops_forever() -> None:
    # 0,1,2 → 60,120,300; 3 wraps back to 60 (the user-locked 1/2/5 cycle).
    assert delay_for_attempt(0) == DELAY_CYCLE[0] == 60
    assert delay_for_attempt(1) == DELAY_CYCLE[1] == 120
    assert delay_for_attempt(2) == DELAY_CYCLE[2] == 300
    assert delay_for_attempt(3) == 60
    assert delay_for_attempt(10) == DELAY_CYCLE[10 % 3]


def test_add_persists_and_increments(tmp_path: Path) -> None:
    meeting = tmp_path / "2026-05-14_1140_x"
    meeting.mkdir()
    q = RetryQueue()

    e1 = q.add(meeting, "APIConnectionError", "transcribe")
    assert e1.attempts == 1
    assert e1.stage == "transcribe"

    state_file = meeting / ".retry-state.json"
    on_disk = json.loads(state_file.read_text())
    assert on_disk["attempts"] == 1
    assert on_disk["last_error"] == "APIConnectionError"
    assert on_disk["stage"] == "transcribe"

    e2 = q.add(meeting, "APIConnectionError again", "transcribe")
    assert e2.attempts == 2
    # next_attempt_at uses the 2nd delay slot (120s) from now
    assert e2.next_attempt_at > time.time() + 60


def test_mark_success_removes_state(tmp_path: Path) -> None:
    meeting = tmp_path / "x"
    meeting.mkdir()
    q = RetryQueue()
    q.add(meeting, "boom", "transcribe")
    assert (meeting / ".retry-state.json").is_file()
    q.mark_success(meeting)
    assert not (meeting / ".retry-state.json").exists()
    assert q.pending() == []


def test_due_respects_next_attempt_at(tmp_path: Path) -> None:
    meeting = tmp_path / "x"
    meeting.mkdir()
    q = RetryQueue()
    entry = q.add(meeting, "boom", "transcribe")
    # First-failure delay is 60s — definitely not due yet.
    assert q.due() == []
    # Force it due and re-check.
    entry.next_attempt_at = time.time() - 1
    # Persist via internal save so scan() would also see this state.
    from meetcap.tray.retry import _save

    _save(entry)
    due = q.due()
    assert len(due) == 1
    assert due[0].meeting_dir == meeting


def test_scan_rebuilds_from_disk(tmp_path: Path) -> None:
    meeting = tmp_path / "2026-05-14_1140_x"
    meeting.mkdir()
    (meeting / ".retry-state.json").write_text(
        json.dumps(
            {
                "attempts": 3,
                "next_attempt_at": time.time() + 60,
                "last_error": "ServerNotFoundError",
                "stage": "upload",
            }
        )
    )
    q = RetryQueue()
    q.scan(tmp_path)
    pending = q.pending()
    assert len(pending) == 1
    assert pending[0].attempts == 3
    assert pending[0].stage == "upload"


def test_scan_seeds_failed_meeting_without_state(tmp_path: Path) -> None:
    """A failure that predates the retry queue (audio.flac + .transcribe-error.log,
    no .retry-state.json) must be picked up on tray startup."""
    meeting = tmp_path / "2026-05-14_1140_x"
    meeting.mkdir()
    (meeting / "audio.flac").write_bytes(b"\x00")
    (meeting / ".transcribe-error.log").write_text("APIConnectionError: Connection error.\n")
    q = RetryQueue()
    q.scan(tmp_path)
    pending = q.pending()
    assert len(pending) == 1
    assert pending[0].meeting_dir == meeting
    assert "Connection" in pending[0].last_error
    # Seeded entries are due immediately so they fire on the very next retry tick.
    assert pending[0].next_attempt_at <= time.time() + 1


def test_scan_skips_completed_meetings(tmp_path: Path) -> None:
    meeting = tmp_path / "done"
    meeting.mkdir()
    (meeting / "audio.flac").write_bytes(b"\x00")
    (meeting / "transcript.json").write_text("{}")
    (meeting / ".drive-upload.json").write_text("{}")
    q = RetryQueue()
    q.scan(tmp_path)
    assert q.pending() == []


def test_scan_skips_in_progress_recording(tmp_path: Path) -> None:
    """A recording in flight has audio.flac but no error log yet — don't queue it
    until it's been quiet for ORPHAN_IDLE_SECONDS."""
    meeting = tmp_path / "recording"
    meeting.mkdir()
    (meeting / "audio.flac").write_bytes(b"\x00")
    # Fresh mtime — within the idle threshold, so still considered live.
    q = RetryQueue()
    q.scan(tmp_path)
    assert q.pending() == []


def test_scan_seeds_orphaned_audio_after_idle(tmp_path: Path) -> None:
    """audio.flac present, no transcript, no log, but mtime > 60 s old:
    seed it. This is the 'tray was killed mid-recording before post-process
    spawned' / 'ffmpeg died silently and we never noticed' case."""
    import os

    from meetcap.tray.retry import ORPHAN_IDLE_SECONDS

    meeting = tmp_path / "orphan"
    meeting.mkdir()
    flac = meeting / "audio.flac"
    flac.write_bytes(b"\x00")
    past = time.time() - (ORPHAN_IDLE_SECONDS + 30)
    os.utime(flac, (past, past))
    q = RetryQueue()
    q.scan(tmp_path)
    pending = q.pending()
    assert len(pending) == 1
    assert pending[0].meeting_dir == meeting
    assert pending[0].stage == "transcribe"


def test_force_due_now(tmp_path: Path) -> None:
    meeting = tmp_path / "x"
    meeting.mkdir()
    q = RetryQueue()
    q.add(meeting, "boom", "transcribe")
    assert q.due() == []  # 60s in the future
    q.force_due_now()
    assert len(q.due()) == 1


def test_detect_failed_stage(tmp_path: Path) -> None:
    meeting = tmp_path / "m"
    meeting.mkdir()
    assert detect_failed_stage(meeting) == "transcribe"
    (meeting / "transcript.json").write_text("{}")
    assert detect_failed_stage(meeting) == "upload"


def test_tail_error_handles_missing_file(tmp_path: Path) -> None:
    assert tail_error(tmp_path / "nonexistent.log") == "unknown"
    log = tmp_path / "log"
    log.write_text("first\n\nlast line that matters\n")
    assert tail_error(log) == "last line that matters"


def test_is_permanent_error_matches_known_patterns() -> None:
    # Real-world examples drawn from observed retry-queue last_error values.
    assert is_permanent_error("audio duration 2725s is longer than 1400s")
    assert is_permanent_error(
        "BadRequestError: Error code: 400 - {'error': {'type': 'invalid_request_error'}}"
    )
    assert is_permanent_error("AuthenticationError: Incorrect API key provided: sk-…")
    assert is_permanent_error("insufficient_quota")
    # And transient ones must NOT match — those should keep cycling.
    assert not is_permanent_error("APIConnectionError: Connection error.")
    assert not is_permanent_error("timeout after 600s")
    assert not is_permanent_error("ServerNotFoundError: Unable to find the server")


def test_add_abandons_on_permanent_error(tmp_path: Path) -> None:
    """A permanent-error pattern must mark the entry abandoned on first failure;
    no cycling, no further work."""
    meeting = tmp_path / "2026-05-14_1200_x"
    meeting.mkdir()
    q = RetryQueue()
    entry = q.add(meeting, PERMANENT_ERROR_PATTERNS[0], "transcribe")
    assert entry.abandoned is True
    assert q.active() == []
    assert q.abandoned() == [entry]
    assert q.due() == []  # never due, even after time passes


def test_add_abandons_at_max_attempts(tmp_path: Path) -> None:
    """Backstop: even on transient-looking errors, abandon after MAX attempts.

    Stops infinite loops from repeating 10-minute timeouts forever."""
    meeting = tmp_path / "m"
    meeting.mkdir()
    q = RetryQueue()
    last = None
    for _ in range(MAX_ATTEMPTS_BEFORE_ABANDON):
        last = q.add(meeting, "timeout after 600s", "transcribe")
    assert last is not None
    assert last.abandoned is True
    assert last.attempts == MAX_ATTEMPTS_BEFORE_ABANDON


def test_due_skips_abandoned(tmp_path: Path) -> None:
    meeting = tmp_path / "m"
    meeting.mkdir()
    q = RetryQueue()
    entry = q.add(meeting, "is longer than 1400 seconds", "transcribe")
    assert entry.abandoned
    entry.next_attempt_at = time.time() - 1  # force due time-wise
    assert q.due() == []  # but abandoned filter wins
    assert q.next_due_in() is None  # no active entries to be due


def test_force_due_now_unabandons(tmp_path: Path) -> None:
    """Manual 'Retry pending now' is the escape hatch after the user fixes
    the underlying problem (e.g., adds API credits)."""
    meeting = tmp_path / "m"
    meeting.mkdir()
    q = RetryQueue()
    q.add(meeting, "is longer than 1400 seconds", "transcribe")
    assert q.abandoned() and not q.active()
    q.force_due_now()
    pending = q.pending()
    assert len(pending) == 1
    assert pending[0].abandoned is False
    assert q.due() == pending  # immediately due now


def test_scan_drops_retry_state_for_completed_meeting(tmp_path: Path) -> None:
    """A manual `meetcap transcribe` may finish a meeting outside the tray's
    awareness, leaving a stale .retry-state.json behind. scan() must clean it
    up rather than re-queue an already-complete meeting."""
    meeting = tmp_path / "2026-05-14_1200_x"
    meeting.mkdir()
    (meeting / "audio.flac").write_bytes(b"\x00")
    (meeting / "transcript.json").write_text("{}")
    (meeting / ".drive-upload.json").write_text("{}")
    (meeting / ".retry-state.json").write_text(
        json.dumps(
            {
                "attempts": 5,
                "next_attempt_at": time.time(),
                "last_error": "transient",
                "stage": "transcribe",
                "abandoned": False,
            }
        )
    )
    q = RetryQueue()
    q.scan(tmp_path)
    assert q.pending() == []
    assert not (meeting / ".retry-state.json").exists()


def test_scan_preserves_abandoned(tmp_path: Path) -> None:
    """abandoned flag must survive a tray restart so we don't accidentally
    resume retrying a meeting we already decided was hopeless."""
    meeting = tmp_path / "2026-05-14_1200_x"
    meeting.mkdir()
    (meeting / ".retry-state.json").write_text(
        json.dumps(
            {
                "attempts": 1,
                "next_attempt_at": time.time(),
                "last_error": "is longer than 1400 seconds",
                "stage": "transcribe",
                "abandoned": True,
            }
        )
    )
    q = RetryQueue()
    q.scan(tmp_path)
    pending = q.pending()
    assert len(pending) == 1
    assert pending[0].abandoned is True
    assert q.due() == []


def test_tail_error_prefers_structured_error_log(tmp_path: Path) -> None:
    """Rich wraps the post-process log; the structured .transcribe-error.log
    next to it has the clean 'ExceptionType: message' line we actually want."""
    meeting = tmp_path / "m"
    meeting.mkdir()
    post = meeting / ".post-process.log"
    post.write_text(
        "mixdown audio.flac → audio.mono.ogg\n"
        "transcribe failed: APIConnectionError: Connection error.\n"
        "see \n"
        "/home/u/Recordings/meetcap/2026-05-14_1140_x/.transcribe-error.log\n"
    )
    (meeting / ".transcribe-error.log").write_text("APIConnectionError: Connection error.\n")
    assert tail_error(post) == "APIConnectionError: Connection error."
