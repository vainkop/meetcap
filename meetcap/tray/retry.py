from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Cycling backoff: 1 min, 2 min, 5 min, then loops forever. Cycle was chosen
# so transient OpenAI/Drive outages clear within ~2 min, and the worst case
# still re-tries every 5 min instead of escalating into 30-min waits.
DELAY_CYCLE: tuple[int, ...] = (60, 120, 300)
RETRY_STATE_FILE = ".retry-state.json"
# Sentinel written by the merge tool. Meetings carrying this marker are
# subsumed by a merged meeting and must be skipped by the retry queue —
# otherwise we'd transcribe + upload them in parallel with the merged one.
MERGED_INTO_FILE = ".merged-into.json"
# Backstop for non-pattern-matched failures so a meeting with persistent
# trouble (e.g. recurring timeouts) doesn't cycle infinitely and burn API
# credits / GPU cycles. With the 1/2/5 cycle, 20 attempts is ~80 min of
# clock time including waits. "Retry pending now" re-arms abandoned
# entries, so this is a soft cap — the user can always force another try.
MAX_ATTEMPTS_BEFORE_ABANDON = 20
# Substrings (case-insensitive) that mark a failure as definitely permanent —
# retrying will produce the same error every time. We abandon on first hit
# rather than waiting for MAX_ATTEMPTS so we don't waste any further work.
# Patterns are matched against the last-error line read from the structured
# .transcribe-error.log when available, or the tail of .post-process.log.
PERMANENT_ERROR_PATTERNS: tuple[str, ...] = (
    "is longer than",  # OpenAI: "audio duration X is longer than Y"
    "invalid_request_error",  # OpenAI: HTTP 400, request-level validation
    "unsupported file format",
    "incorrect api key",
    "invalid authentication",
    "insufficient credits",
    "insufficient_quota",
    "model_not_found",
    "no audio.flac",  # file missing — won't reappear on its own
    "permission_denied",  # auth / scope issues
)


def is_permanent_error(error: str) -> bool:
    """True if the error message matches a known non-recoverable failure."""
    lowered = error.lower()
    return any(p in lowered for p in PERMANENT_ERROR_PATTERNS)


@dataclass
class RetryEntry:
    meeting_dir: Path
    attempts: int  # failed attempts so far (1 = first failure)
    next_attempt_at: float  # unix epoch seconds
    last_error: str
    stage: str  # "transcribe" or "upload"
    # Set when retries should stop: either a permanent error matched a
    # PERMANENT_ERROR_PATTERN, or attempts hit MAX_ATTEMPTS_BEFORE_ABANDON.
    # Abandoned entries stay in the queue (so UI can show them) but the
    # retry thread skips them. force_due_now() un-abandons every entry,
    # giving the user a "Retry now" escape hatch.
    abandoned: bool = False


def delay_for_attempt(attempts: int) -> int:
    """Return the seconds-to-wait before attempt #(attempts+1). 0-indexed."""
    return DELAY_CYCLE[max(0, attempts) % len(DELAY_CYCLE)]


class RetryQueue:
    """Thread-safe queue + on-disk persistence (one .retry-state.json per meeting)."""

    def __init__(self) -> None:
        self._entries: dict[Path, RetryEntry] = {}
        self._lock = threading.Lock()

    def scan(self, recordings_root: Path) -> None:
        """Rebuild from disk on tray startup. Also seeds entries for old failures
        that predate the retry queue (audio.flac present, transcript.json absent,
        .retry-state.json absent) so a tray restart picks them up."""
        if not recordings_root.is_dir():
            return
        with self._lock:
            for d in sorted(recordings_root.glob("*")):
                if not d.is_dir():
                    continue
                if (d / MERGED_INTO_FILE).is_file():
                    # Sub-segment of a merged meeting. Drop any stale
                    # .retry-state.json so we don't keep re-trying it.
                    (d / RETRY_STATE_FILE).unlink(missing_ok=True)
                    continue
                state_path = d / RETRY_STATE_FILE
                if state_path.is_file():
                    # Meeting may have completed via a manual `meetcap
                    # transcribe` outside the tray's awareness — drop the
                    # stale marker so we don't keep tracking a done meeting.
                    if (d / "transcript.json").is_file() and (d / ".drive-upload.json").is_file():
                        state_path.unlink(missing_ok=True)
                        continue
                    entry = _load(state_path, d)
                    if entry is not None:
                        self._entries[d] = entry
                    continue
                if _looks_like_failed_meeting(d):
                    entry = RetryEntry(
                        meeting_dir=d,
                        attempts=0,
                        next_attempt_at=time.time(),  # due immediately
                        last_error=_seed_error_message(d),
                        stage="transcribe",
                    )
                    self._entries[d] = entry
                    _save(entry)

    def add(self, meeting_dir: Path, last_error: str, stage: str) -> RetryEntry:
        """Record a failure and arm the next retry. attempts increments on each call.

        Abandoned outcomes:
        - Permanent error pattern matched → abandon immediately, no further retry.
        - attempts hits MAX_ATTEMPTS_BEFORE_ABANDON → abandon as a backstop.
        - Meeting was merged elsewhere → entry dropped (content moved).

        Abandoned entries remain in the queue so the UI can surface them; the
        retry thread skips them. force_due_now() un-abandons everything for
        manual one-shot retries."""
        if (meeting_dir / MERGED_INTO_FILE).is_file():
            with self._lock:
                self._entries.pop(meeting_dir, None)
            (meeting_dir / RETRY_STATE_FILE).unlink(missing_ok=True)
            return RetryEntry(meeting_dir, 0, time.time(), "merged; not queued", stage)
        with self._lock:
            existing = self._entries.get(meeting_dir)
            attempts = (existing.attempts + 1) if existing else 1
            abandoned = is_permanent_error(last_error) or attempts >= MAX_ATTEMPTS_BEFORE_ABANDON
            entry = RetryEntry(
                meeting_dir=meeting_dir,
                attempts=attempts,
                next_attempt_at=time.time() + delay_for_attempt(attempts - 1),
                last_error=last_error,
                stage=stage,
                abandoned=abandoned,
            )
            self._entries[meeting_dir] = entry
            _save(entry)
            return entry

    def mark_success(self, meeting_dir: Path) -> None:
        with self._lock:
            self._entries.pop(meeting_dir, None)
        (meeting_dir / RETRY_STATE_FILE).unlink(missing_ok=True)

    def due(self) -> list[RetryEntry]:
        """Entries whose next_attempt_at has passed AND which aren't abandoned."""
        now = time.time()
        with self._lock:
            return [
                e for e in self._entries.values() if not e.abandoned and e.next_attempt_at <= now
            ]

    def pending(self) -> list[RetryEntry]:
        """All entries currently in the queue (active + abandoned)."""
        with self._lock:
            return list(self._entries.values())

    def active(self) -> list[RetryEntry]:
        """Entries still being retried (excludes abandoned)."""
        with self._lock:
            return [e for e in self._entries.values() if not e.abandoned]

    def abandoned(self) -> list[RetryEntry]:
        """Entries that hit a permanent error or the max-attempts cap."""
        with self._lock:
            return [e for e in self._entries.values() if e.abandoned]

    def next_due_in(self) -> float | None:
        """Seconds until the next non-abandoned entry is due. None if nothing
        is still actively scheduled."""
        with self._lock:
            candidates = [e.next_attempt_at for e in self._entries.values() if not e.abandoned]
        if not candidates:
            return None
        return max(0.0, min(candidates) - time.time())

    def force_due_now(self) -> None:
        """Arm every queued entry to fire on the next tick. Also un-abandons —
        this is the manual escape hatch from a permanent-error or max-attempts
        abandonment, e.g. after the user has fixed the underlying problem."""
        with self._lock:
            now = time.time()
            for entry in self._entries.values():
                entry.next_attempt_at = now
                entry.abandoned = False
                _save(entry)


def _save(entry: RetryEntry) -> None:
    (entry.meeting_dir / RETRY_STATE_FILE).write_text(
        json.dumps(
            {
                "attempts": entry.attempts,
                "next_attempt_at": entry.next_attempt_at,
                "last_error": entry.last_error,
                "stage": entry.stage,
                "abandoned": entry.abandoned,
            },
            indent=2,
        )
    )


def _load(state_path: Path, meeting_dir: Path) -> RetryEntry | None:
    try:
        data = json.loads(state_path.read_text())
        return RetryEntry(
            meeting_dir=meeting_dir,
            attempts=int(data["attempts"]),
            next_attempt_at=float(data["next_attempt_at"]),
            last_error=str(data.get("last_error", "")),
            stage=str(data.get("stage", "transcribe")),
            abandoned=bool(data.get("abandoned", False)),
        )
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


ORPHAN_IDLE_SECONDS = 60
"""How long audio.flac must have been untouched before scan() treats a
meeting with no post-process log as orphaned (tray was killed before
post-process started, or ffmpeg died silently and we never noticed).
60 s is comfortably longer than the watch loop's 2 s poll cycle so we
don't accidentally pick up a recording that's actively being written."""


def _looks_like_failed_meeting(meeting_dir: Path) -> bool:
    flac = meeting_dir / "audio.flac"
    tx_json = meeting_dir / "transcript.json"
    drive_state = meeting_dir / ".drive-upload.json"
    if not flac.is_file():
        return False
    if tx_json.is_file() and drive_state.is_file():
        return False  # fully done
    # Either no transcript (transcribe failed) or no upload (upload failed).
    if (meeting_dir / ".transcribe-error.log").is_file():
        return True
    if (meeting_dir / ".post-process.log").is_file():
        return True
    # Orphaned recording: audio.flac present but nothing ever post-processed
    # it (tray killed mid-recording, ffmpeg died silently, …). Only seed once
    # the file has been quiet for ORPHAN_IDLE_SECONDS so we don't fight an
    # in-flight ffmpeg that's still writing to it.
    return time.time() - flac.stat().st_mtime > ORPHAN_IDLE_SECONDS


def _seed_error_message(meeting_dir: Path) -> str:
    err_log = meeting_dir / ".transcribe-error.log"
    if err_log.is_file():
        try:
            return err_log.read_text().strip().splitlines()[-1]
        except (OSError, IndexError):
            pass
    return "previous attempt failed"


def detect_failed_stage(meeting_dir: Path) -> str:
    """Inspect on-disk artifacts to decide whether transcribe or upload failed."""
    if (meeting_dir / "transcript.json").is_file():
        return "upload"
    return "transcribe"


def tail_error(log_path: Path, max_chars: int = 200) -> str:
    """Prefer the structured .transcribe-error.log next to the post-process log
    (single 'ErrorType: message' line) — falling back to the last non-empty line
    of the post-process log itself. Rich wraps long lines in the post-process
    log, so naïvely tailing it can yield a truncated path fragment instead of
    the actual exception."""
    err_log = log_path.parent / ".transcribe-error.log"
    if err_log.is_file():
        try:
            txt = err_log.read_text().strip()
            if txt:
                return txt.splitlines()[-1][-max_chars:]
        except OSError:
            pass
    try:
        lines = [ln.strip() for ln in log_path.read_text().splitlines() if ln.strip()]
    except OSError:
        return "unknown"
    if not lines:
        return "unknown"
    return lines[-1][-max_chars:]
