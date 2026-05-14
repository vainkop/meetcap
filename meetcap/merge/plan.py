from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# A meeting that was last written within this window is treated as "live" — we
# refuse to merge it because its audio.flac is still being appended to by an
# active ffmpeg. 30 s is comfortable above the watch loop's 2 s poll cycle.
ACTIVE_RECORDING_THRESHOLD_SECONDS = 30

# Dir name pattern: YYYY-MM-DD_HHMM_<slug>. Matches the convention from
# `meetcap.audio.paths.build_meeting_dir`.
_DIR_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{4})_(?P<slug>.+)$")


class MergeError(Exception):
    """Raised when a merge request can't be honoured (active recording, missing
    audio, only one source, ...). User-facing messages — safe to surface."""


@dataclass(frozen=True)
class MergeSource:
    """One meeting being folded into the merged output."""

    dir: Path
    audio: Path
    started_at: datetime
    slug: str
    duration_sec: float
    size_bytes: int

    @property
    def name(self) -> str:
        return self.dir.name


@dataclass(frozen=True)
class MergePlan:
    """Validated, chronologically-ordered list of sources + chosen output dir."""

    sources: tuple[MergeSource, ...]
    output_dir: Path

    @classmethod
    def build(cls, dirs: list[Path], custom_slug: str | None = None) -> MergePlan:
        """Parse + validate; raise MergeError if anything's wrong. Sources are
        re-sorted chronologically regardless of input order."""
        if len(dirs) < 2:
            raise MergeError("merge requires at least two meeting directories")
        sources: list[MergeSource] = []
        seen: set[Path] = set()
        now = time.time()
        for d in dirs:
            d = d.expanduser().resolve()
            if d in seen:
                raise MergeError(f"duplicate input: {d.name}")
            seen.add(d)
            if not d.is_dir():
                raise MergeError(f"not a directory: {d}")
            if (d / ".merged-into.json").is_file():
                raise MergeError(f"{d.name} is already merged into another meeting")
            audio = d / "audio.flac"
            if not audio.is_file():
                raise MergeError(f"no audio.flac in {d.name}")
            if now - audio.stat().st_mtime < ACTIVE_RECORDING_THRESHOLD_SECONDS:
                raise MergeError(
                    f"{d.name} appears to be actively recording "
                    "(audio.flac written within the last 30 s)"
                )
            m = _DIR_RE.match(d.name)
            if m is None:
                raise MergeError(f"unparseable meeting dir name: {d.name}")
            started_at = datetime.strptime(m.group("ts"), "%Y-%m-%d_%H%M")
            duration = _probe_duration(audio)
            sources.append(
                MergeSource(
                    dir=d,
                    audio=audio,
                    started_at=started_at,
                    slug=m.group("slug"),
                    duration_sec=duration,
                    size_bytes=audio.stat().st_size,
                )
            )
        ordered = tuple(sorted(sources, key=lambda s: s.started_at))
        slug = custom_slug or f"{ordered[0].slug}-merged"
        ts = ordered[0].started_at.strftime("%Y-%m-%d_%H%M")
        recordings_root = ordered[0].dir.parent
        output_dir = recordings_root / f"{ts}_{slug}"
        if any(output_dir == s.dir for s in ordered):
            # Slug clash with a source — bump it so we don't overwrite.
            output_dir = recordings_root / f"{ts}_{slug}-merged"
        return cls(sources=ordered, output_dir=output_dir)

    @property
    def total_duration_sec(self) -> float:
        return sum(s.duration_sec for s in self.sources)

    @property
    def total_bytes(self) -> int:
        return sum(s.size_bytes for s in self.sources)


def _probe_duration(audio: Path) -> float:
    """ffprobe wrapper — returns 0.0 if probing fails (we still want to merge)."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return 0.0
