from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from rich.console import Console

from meetcap.merge.plan import MergePlan
from meetcap.tray.retry import MERGED_INTO_FILE

MERGED_FROM_FILE = ".merged-from.json"


def write_merge_markers(plan: MergePlan) -> None:
    """Write .merged-into.json on every source and .merged-from.json on the
    merged output dir. Marker writes happen *before* concat so the retry
    queue stops trying to transcribe sources the moment a merge is committed
    — even if concat then fails, sources stay marked. To 'un-merge', delete
    the markers manually."""
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    merged_name = plan.output_dir.name
    for s in plan.sources:
        (s.dir / MERGED_INTO_FILE).write_text(json.dumps({"merged_dir": merged_name}, indent=2))
    (plan.output_dir / MERGED_FROM_FILE).write_text(
        json.dumps(
            {"sources": [s.name for s in plan.sources]},
            indent=2,
        )
    )


def run_concat(plan: MergePlan, console: Console) -> Path:
    """Run ffmpeg's concat demuxer over plan.sources and produce one FLAC.
    All sources are 16 kHz 2-ch FLAC (recorder.py invariant) so `-c copy`
    works — no re-encode, no quality loss. Idempotent: skips if the merged
    FLAC already exists."""
    out_flac = plan.output_dir / "audio.flac"
    if out_flac.is_file() and out_flac.stat().st_size > 0:
        console.print(f"[dim]reusing existing[/dim] {out_flac.name}")
        return out_flac

    plan.output_dir.mkdir(parents=True, exist_ok=True)
    # Concat demuxer wants a list file with `file '<path>'` lines, one per input.
    list_file = plan.output_dir / ".concat-list.txt"
    list_file.write_text(
        "\n".join(f"file {shlex.quote(str(s.audio))}" for s in plan.sources) + "\n"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        "-y",
        str(out_flac),
    ]
    console.print(f"[cyan]ffmpeg concat[/cyan] → {out_flac.name}")
    try:
        subprocess.run(cmd, check=True)
    finally:
        list_file.unlink(missing_ok=True)
    return out_flac
