from __future__ import annotations

from datetime import datetime
from pathlib import Path

from slugify import slugify

RECORDINGS_ROOT = Path.home() / "Recordings" / "meetcap"


def build_meeting_dir(slug: str, when: datetime | None = None) -> Path:
    """Return (and create) ~/Recordings/meetcap/<YYYY-MM-DD_HHMM>_<slug>/."""
    ts = (when or datetime.now()).strftime("%Y-%m-%d_%H%M")
    safe_slug = slugify(slug) or "untitled"
    path = RECORDINGS_ROOT / f"{ts}_{safe_slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path
