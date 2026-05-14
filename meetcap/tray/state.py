from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TrayState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    MERGING = "merging"
    TRANSCRIBING = "transcribing"
    UPLOADING = "uploading"
    # Set after a post-process failure while the RetryQueue waits for the next
    # attempt. Visually amber; distinct from ERROR which is for non-recoverable
    # local issues (e.g. audio source resolution).
    RETRY_PENDING = "retry_pending"
    ERROR = "error"


@dataclass(frozen=True)
class StateUpdate:
    state: TrayState
    detail: str = ""  # short human-readable detail line for the menu
