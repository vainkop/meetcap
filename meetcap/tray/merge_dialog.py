from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk  # noqa: E402

from meetcap.audio.paths import RECORDINGS_ROOT  # noqa: E402
from meetcap.merge.plan import ACTIVE_RECORDING_THRESHOLD_SECONDS  # noqa: E402
from meetcap.tray.retry import MERGED_INTO_FILE  # noqa: E402

_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{4})_(.+)$")


@dataclass(frozen=True)
class _Meeting:
    path: Path
    name: str
    duration_sec: float
    size_bytes: int
    is_active: bool
    is_already_merged: bool


def _format_duration(secs: float) -> str:
    if secs <= 0:
        return "?"
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def _probe_duration(audio: Path) -> float:
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
            timeout=5,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return 0.0


def _discover_meetings(root: Path, limit: int = 15) -> list[_Meeting]:
    """Latest `limit` meeting dirs, newest first. Excludes ones with no
    audio.flac. Marks live recordings + already-merged for the UI to disable."""
    if not root.is_dir():
        return []
    out: list[_Meeting] = []
    candidates = sorted(
        (p for p in root.glob("*") if p.is_dir() and _DIR_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    now = time.time()
    for d in candidates:
        audio = d / "audio.flac"
        if not audio.is_file():
            continue
        st = audio.stat()
        out.append(
            _Meeting(
                path=d,
                name=d.name,
                duration_sec=_probe_duration(audio),
                size_bytes=st.st_size,
                is_active=(now - st.st_mtime) < ACTIVE_RECORDING_THRESHOLD_SECONDS,
                is_already_merged=(d / MERGED_INTO_FILE).is_file(),
            )
        )
        if len(out) >= limit:
            break
    return out


class MergeDialog(Gtk.Window):  # type: ignore[misc]
    """Modal-ish picker for choosing which meeting dirs to concat.
    The dialog is non-modal (no Gtk.Dialog because AppIndicator doesn't have a
    parent window to attach to), but `set_keep_above(True)` keeps it visible
    and `set_position(CENTER)` anchors it. Callers get the list of selected
    paths + optional slug via `on_submit`."""

    def __init__(
        self,
        on_submit: Callable[[list[Path], str | None], None],
    ) -> None:
        super().__init__(title="Merge meetings")
        self._on_submit = on_submit
        self.set_default_size(560, 460)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_resizable(True)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_border_width(10)

        header = Gtk.Label()
        header.set_markup(
            "<b>Pick two or more meetings to concat into one.</b>\n"
            "<small>Ordered chronologically. Sources stay on disk but are marked merged.</small>"
        )
        header.set_xalign(0.0)
        header.set_line_wrap(True)
        vbox.pack_start(header, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.set_min_content_height(280)

        list_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._checks: list[tuple[Gtk.CheckButton, _Meeting]] = []
        meetings = _discover_meetings(RECORDINGS_ROOT)
        if not meetings:
            empty = Gtk.Label(label="No meetings found under ~/Recordings/meetcap/.")
            empty.set_xalign(0.0)
            list_vbox.pack_start(empty, False, False, 4)
        for m in meetings:
            mb = m.size_bytes / (1024 * 1024)
            label = f"{m.name}  ·  {_format_duration(m.duration_sec)}  ·  {mb:.1f} MB"
            suffixes = []
            if m.is_active:
                suffixes.append("recording")
            if m.is_already_merged:
                suffixes.append("already merged")
            if suffixes:
                label += f"  ({', '.join(suffixes)})"
            chk = Gtk.CheckButton.new_with_label(label)
            if m.is_active or m.is_already_merged:
                chk.set_sensitive(False)
            list_vbox.pack_start(chk, False, False, 0)
            self._checks.append((chk, m))
        scroll.add(list_vbox)
        vbox.pack_start(scroll, True, True, 0)

        slug_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        slug_box.pack_start(Gtk.Label(label="Slug:"), False, False, 0)
        self._slug_entry = Gtk.Entry()
        self._slug_entry.set_placeholder_text("optional · default: <first-slug>-merged")
        slug_box.pack_start(self._slug_entry, True, True, 0)
        vbox.pack_start(slug_box, False, False, 0)

        self._error_label = Gtk.Label()
        self._error_label.set_xalign(0.0)
        self._error_label.set_no_show_all(True)
        vbox.pack_start(self._error_label, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cancel_btn = Gtk.Button.new_with_label("Cancel")
        cancel_btn.connect("clicked", lambda _: self.destroy())
        merge_btn = Gtk.Button.new_with_label("Merge && queue")
        merge_btn.get_style_context().add_class("suggested-action")
        merge_btn.connect("clicked", self._handle_submit)
        btn_box.pack_end(merge_btn, False, False, 0)
        btn_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(btn_box, False, False, 0)

        self.add(vbox)
        self.show_all()
        self._error_label.hide()

    def _handle_submit(self, _btn: Gtk.Button) -> None:
        selected = [m.path for chk, m in self._checks if chk.get_active()]
        if len(selected) < 2:
            self._show_error("Select at least two meetings to merge.")
            return
        slug = self._slug_entry.get_text().strip() or None
        try:
            self._on_submit(selected, slug)
        except Exception as e:  # surface validation errors inline
            self._show_error(f"{type(e).__name__}: {e}")
            return
        self.destroy()

    def _show_error(self, text: str) -> None:
        self._error_label.set_markup(f"<span color='#e53935'>{text}</span>")
        self._error_label.set_no_show_all(False)
        self._error_label.show()
