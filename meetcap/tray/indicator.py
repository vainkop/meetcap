from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")

from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from meetcap.audio.paths import RECORDINGS_ROOT  # noqa: E402
from meetcap.config import (  # noqa: E402
    EXAMPLE_CONFIG_PATH,
    USER_CONFIG_PATH,
    Settings,
    update_user_config,
)
from meetcap.tray.state import TrayState  # noqa: E402

# Transcription backend choices (cloud + local Whisper variants).
# Tuple shape: (backend, model, label) where model is empty for cloud
# (we pin to `gpt-4o-transcribe-diarize` server-side).
CLOUD_TRANSCRIBE_CHOICES: list[tuple[str, str, str]] = [
    ("openai", "gpt-4o-transcribe-diarize", "gpt-4o-transcribe-diarize  (cloud, w/ diarize)"),
]
LOCAL_WHISPER_CHOICES: list[tuple[str, str, str]] = [
    ("local", "large-v3", "large-v3         (best, ~3 min/hr)"),
    ("local", "large-v3-turbo", "large-v3-turbo   (~8× faster, slight regress)"),
    ("local", "medium", "medium           (smaller VRAM)"),
    ("local", "small", "small            (smallest, fastest)"),
]
# Cloud summary entries are always shown; local Ollama entries are
# discovered at tray-startup time (see `_discover_ollama_models`).
CLOUD_SUMMARY_CHOICES: list[tuple[str, str, str]] = [
    ("openai", "gpt-5.4-mini", "gpt-5.4-mini   (cloud, cheap, default)"),
    ("openai", "gpt-5.5", "gpt-5.5   (cloud, best, ~7× $)"),
    ("openai", "gpt-5.4-nano", "gpt-5.4-nano  (cloud, floor cheap)"),
]


def _discover_ollama_models() -> list[tuple[str, str, str]]:
    """Return (provider, model, label) for each locally-pulled Ollama model.
    Empty list if Ollama isn't reachable on localhost:11434."""
    import json
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        with urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
            data = json.loads(resp.read())
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    out: list[tuple[str, str, str]] = []
    for m in data.get("models", []) or []:
        name = m.get("name", "")
        if not name:
            continue
        size_gb = float(m.get("size") or 0) / (1024**3)
        out.append(("ollama", name, f"{name}   (local · Ollama, {size_gb:.1f} GB)"))
    return out


def build_summary_choices() -> list[tuple[str, str, str]]:
    """Static cloud entries + dynamically-discovered local Ollama entries."""
    return CLOUD_SUMMARY_CHOICES + _discover_ollama_models()


LANGUAGE_CHOICES: list[tuple[str | None, str]] = [
    (None, "Auto-detect"),
    ("en", "English"),
    ("he", "Hebrew"),
    ("ru", "Russian"),
]

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_ICON_FOR = {
    TrayState.IDLE: "idle",
    TrayState.RECORDING: "recording",
    TrayState.MERGING: "merging",
    TrayState.TRANSCRIBING: "transcribing",
    TrayState.UPLOADING: "uploading",
    TrayState.RETRY_PENDING: "pending",
    TrayState.ERROR: "error",
}
ICON_PATHS = {state: str(ASSETS_DIR / f"meetcap-{name}.svg") for state, name in _ICON_FOR.items()}
# MERGING / TRANSCRIBING / UPLOADING blink: alternate between the primary
# icon and a faded "-alt" companion every BLINK_INTERVAL_MS.
ICON_ALT_PATHS = {
    TrayState.MERGING: str(ASSETS_DIR / "meetcap-merging-alt.svg"),
    TrayState.TRANSCRIBING: str(ASSETS_DIR / "meetcap-transcribing-alt.svg"),
    TrayState.UPLOADING: str(ASSETS_DIR / "meetcap-uploading-alt.svg"),
}
BLINK_STATES = frozenset({TrayState.MERGING, TrayState.TRANSCRIBING, TrayState.UPLOADING})
BLINK_INTERVAL_MS = 500
# How often to refresh the State menu label so the "next retry in M:SS"
# countdown stays current. 5 s is fine-grained enough without spamming GTK.
RETRY_TICK_SECONDS = 5

STATE_LABEL = {
    TrayState.IDLE: "Idle",
    TrayState.RECORDING: "Recording",
    TrayState.MERGING: "Merging",
    TrayState.TRANSCRIBING: "Transcribing",
    TrayState.UPLOADING: "Uploading",
    TrayState.RETRY_PENDING: "Retry pending",
    TrayState.ERROR: "Error",
}


def _xdg_open(path: str) -> None:
    subprocess.Popen(
        ["xdg-open", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _open_user_config() -> None:
    """Open ~/.config/meetcap/config.toml — seed from the example on first use."""
    if not USER_CONFIG_PATH.exists():
        USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if EXAMPLE_CONFIG_PATH.exists():
            USER_CONFIG_PATH.write_text(EXAMPLE_CONFIG_PATH.read_text())
        else:
            USER_CONFIG_PATH.touch()
    _xdg_open(str(USER_CONFIG_PATH))


class TrayIndicator:
    """AppIndicator-backed tray UI + 9-item menu. Thread-safe via GLib.idle_add."""

    def __init__(
        self,
        on_manual_toggle: Callable[[], None],
        on_auto_toggle: Callable[[bool], None],
        on_enroll: Callable[[], None],
        on_quit: Callable[[], None],
        settings: Settings,
        auto_detect_initial: bool = True,
    ) -> None:
        self._on_manual_toggle = on_manual_toggle
        self._on_auto_toggle = on_auto_toggle
        self._on_enroll = on_enroll
        self._on_quit = on_quit
        self._settings = settings
        self._state = TrayState.IDLE
        self._detail = ""
        self._blink_id: int | None = None
        self._blink_alt = False
        self._retry_status_provider: Callable[[], str | None] | None = None
        self._on_retry_now: Callable[[], None] | None = None
        self._on_open_merge_dialog: Callable[[], None] | None = None

        self.indicator = AppIndicator.Indicator.new(
            "meetcap",
            ICON_PATHS[TrayState.IDLE],
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("meetcap")

        self._state_item: Gtk.MenuItem = Gtk.MenuItem.new_with_label("State: Idle")
        self._state_item.set_sensitive(False)
        self._retry_now_item: Gtk.MenuItem = Gtk.MenuItem.new_with_label("Retry pending now")
        self._retry_now_item.connect("activate", self._handle_retry_now)
        self._retry_now_item.set_sensitive(False)
        self._retry_now_item.set_visible(False)
        self._record_item: Gtk.MenuItem = Gtk.MenuItem.new_with_label("● Start recording")
        self._record_item.connect("activate", self._handle_manual_toggle)
        self._auto_item: Gtk.CheckMenuItem = Gtk.CheckMenuItem.new_with_label("Auto-detect")
        self._auto_item.set_active(auto_detect_initial)
        self._auto_item.connect("toggled", self._handle_auto_toggle)
        self._recent_submenu: Gtk.Menu = Gtk.Menu()
        recent_item = Gtk.MenuItem.new_with_label("Recent meetings")
        recent_item.set_submenu(self._recent_submenu)
        self._recent_root_item = recent_item
        # "Merge meetings…" stays appended at the end of Recent meetings ▶.
        # Caching the widget so _refresh_recent doesn't drop it when rebuilding.
        self._merge_dialog_item: Gtk.MenuItem = Gtk.MenuItem.new_with_label("Merge meetings…")
        self._merge_dialog_item.connect("activate", self._handle_open_merge)
        self._refresh_recent()

        open_folder_item = Gtk.MenuItem.new_with_label("Open recordings folder")
        open_folder_item.connect("activate", lambda _: _xdg_open(str(RECORDINGS_ROOT)))
        enroll_item = Gtk.MenuItem.new_with_label("Enroll voice…")
        enroll_item.connect("activate", lambda _: self._on_enroll())
        config_item = Gtk.MenuItem.new_with_label("Open config…")
        config_item.connect("activate", lambda _: _open_user_config())
        quit_item = Gtk.MenuItem.new_with_label("Quit")
        quit_item.connect("activate", lambda _: self._on_quit())

        settings_item = self._build_settings_menu()

        self.menu = Gtk.Menu()
        for item in (
            self._state_item,
            self._retry_now_item,
            Gtk.SeparatorMenuItem(),
            self._record_item,
            self._auto_item,
            Gtk.SeparatorMenuItem(),
            self._recent_root_item,
            open_folder_item,
            Gtk.SeparatorMenuItem(),
            settings_item,
            enroll_item,
            config_item,
            Gtk.SeparatorMenuItem(),
            quit_item,
        ):
            self.menu.append(item)
        self.menu.show_all()
        self._retry_now_item.hide()
        self.indicator.set_menu(self.menu)
        GLib.timeout_add_seconds(RETRY_TICK_SECONDS, self._tick_retry_status)

    def _build_settings_menu(self) -> Gtk.MenuItem:
        """Settings submenu: Backend / Summary / Language radio choices."""
        settings_root = Gtk.MenuItem.new_with_label("Settings")
        submenu = Gtk.Menu()

        # --- Transcription Backend submenu ---
        # Cloud rows at top level; local Whisper variants nested under a
        # "Local (faster-whisper) ▶" sub-submenu so the parent stays short.
        backend_root = Gtk.MenuItem.new_with_label("Backend")
        backend_sub = Gtk.Menu()
        current_backend = self._settings.transcription.backend.lower()
        current_cloud_model = self._settings.transcription.model
        current_local_model = self._settings.transcription.local_model
        backend_group: Gtk.RadioMenuItem | None = None
        for backend, model, label in CLOUD_TRANSCRIBE_CHOICES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(backend_group, label)
            if backend_group is None:
                backend_group = item
            if backend == current_backend and model == current_cloud_model:
                item.set_active(True)
            item.connect("toggled", self._on_backend_choice_toggled, backend, model)
            backend_sub.append(item)
        # Nested submenu for local Whisper variants
        local_whisper_root = Gtk.MenuItem.new_with_label("Local (faster-whisper)")
        local_whisper_sub = Gtk.Menu()
        for backend, model, label in LOCAL_WHISPER_CHOICES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(backend_group, label)
            if backend_group is None:
                backend_group = item
            if backend == current_backend and model == current_local_model:
                item.set_active(True)
            item.connect("toggled", self._on_backend_choice_toggled, backend, model)
            local_whisper_sub.append(item)
        local_whisper_root.set_submenu(local_whisper_sub)
        backend_sub.append(Gtk.SeparatorMenuItem())
        backend_sub.append(local_whisper_root)
        backend_root.set_submenu(backend_sub)

        # --- Summary submenu ---
        # Off + cloud rows at top level; Ollama models nested under
        # "Local (Ollama) ▶" so the parent stays short even with many pulls.
        summary_root = Gtk.MenuItem.new_with_label("Summary")
        summary_sub = Gtk.Menu()
        off_item = Gtk.CheckMenuItem.new_with_label("Off")
        off_item.set_active(not self._settings.transcription.summary_enabled)
        off_item.connect("toggled", self._on_summary_off_toggled)
        summary_sub.append(off_item)
        summary_sub.append(Gtk.SeparatorMenuItem())
        current_provider = self._settings.transcription.summary_provider.lower()
        current_summary_model = self._settings.transcription.summary_model
        summary_group: Gtk.RadioMenuItem | None = None
        self._summary_model_items: list[Gtk.RadioMenuItem] = []
        for provider, model, label in CLOUD_SUMMARY_CHOICES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(summary_group, label)
            if summary_group is None:
                summary_group = item
            if provider == current_provider and model == current_summary_model:
                item.set_active(True)
            item.set_sensitive(self._settings.transcription.summary_enabled)
            item.connect("toggled", self._on_summary_choice_toggled, provider, model)
            self._summary_model_items.append(item)
            summary_sub.append(item)
        ollama_models = _discover_ollama_models()
        if ollama_models:
            local_summary_root = Gtk.MenuItem.new_with_label("Local (Ollama)")
            local_summary_sub = Gtk.Menu()
            for provider, model, label in ollama_models:
                item = Gtk.RadioMenuItem.new_with_label_from_widget(summary_group, label)
                if summary_group is None:
                    summary_group = item
                if provider == current_provider and model == current_summary_model:
                    item.set_active(True)
                item.set_sensitive(self._settings.transcription.summary_enabled)
                item.connect("toggled", self._on_summary_choice_toggled, provider, model)
                self._summary_model_items.append(item)
                local_summary_sub.append(item)
            local_summary_root.set_submenu(local_summary_sub)
            summary_sub.append(Gtk.SeparatorMenuItem())
            summary_sub.append(local_summary_root)
        summary_root.set_submenu(summary_sub)

        # --- Language submenu ---
        lang_root = Gtk.MenuItem.new_with_label("Language")
        lang_sub = Gtk.Menu()
        current_lang = self._settings.transcription.language
        lang_group: Gtk.RadioMenuItem | None = None
        for lang_value, label in LANGUAGE_CHOICES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(lang_group, label)
            if lang_group is None:
                lang_group = item
            if lang_value == current_lang:
                item.set_active(True)
            item.connect("toggled", self._on_language_toggled, lang_value)
            lang_sub.append(item)
        lang_root.set_submenu(lang_sub)

        for child in (backend_root, summary_root, lang_root):
            submenu.append(child)
        settings_root.set_submenu(submenu)
        return settings_root

    # --- Settings handlers (write to config.toml + update in-memory) ---

    def _on_backend_choice_toggled(self, item: Gtk.RadioMenuItem, backend: str, model: str) -> None:
        if not item.get_active():
            return
        self._settings.transcription.backend = backend
        update_user_config("transcription", "backend", backend)
        if backend == "local":
            self._settings.transcription.local_model = model
            update_user_config("transcription", "local_model", model)
        else:
            self._settings.transcription.model = model
            update_user_config("transcription", "model", model)

    def _on_summary_off_toggled(self, item: Gtk.CheckMenuItem) -> None:
        enabled = not item.get_active()
        self._settings.transcription.summary_enabled = enabled
        update_user_config("transcription", "summary_enabled", enabled)
        for mi in getattr(self, "_summary_model_items", []):
            mi.set_sensitive(enabled)

    def _on_summary_choice_toggled(
        self, item: Gtk.RadioMenuItem, provider: str, model: str
    ) -> None:
        if not item.get_active():
            return
        self._settings.transcription.summary_provider = provider
        self._settings.transcription.summary_model = model
        update_user_config("transcription", "summary_provider", provider)
        update_user_config("transcription", "summary_model", model)

    def _on_language_toggled(self, item: Gtk.RadioMenuItem, value: str | None) -> None:
        if not item.get_active():
            return
        self._settings.transcription.language = value
        update_user_config("transcription", "language", value)

    # ----- thread-safe accessors (callable from worker threads) -----

    def set_state(self, state: TrayState, detail: str = "") -> None:
        GLib.idle_add(self._apply_state, state, detail)

    def refresh_recent(self) -> None:
        GLib.idle_add(self._refresh_recent)

    def set_retry_status_provider(
        self,
        provider: Callable[[], str | None],
        on_retry_now: Callable[[], None] | None = None,
    ) -> None:
        """Wire a callback that returns a short status string for the queue, plus
        an optional callable that fires every pending retry immediately when the
        user picks 'Retry pending now' from the menu."""
        self._retry_status_provider = provider
        self._on_retry_now = on_retry_now

    def set_merge_handler(self, opener: Callable[[], None]) -> None:
        """Wire the 'Merge meetings…' menu item to a function that opens the
        GTK picker. Kept as a setter so the indicator stays unaware of dialog
        construction details."""
        self._on_open_merge_dialog = opener

    # ----- main-thread handlers -----

    def _apply_state(self, state: TrayState, detail: str) -> bool:
        self._state = state
        self._detail = detail
        self._stop_blink()
        self.indicator.set_icon_full(ICON_PATHS[state], state.value)
        label = STATE_LABEL[state]
        if detail:
            label = f"{label} · {detail}"
        self._state_item.set_label(f"State: {label}")
        if state == TrayState.RECORDING:
            self._record_item.set_label("■ Stop recording")
        else:
            self._record_item.set_label("● Start recording")
        # Manual record toggle stays enabled in every state so the user is
        # never trapped (e.g. by a stale ERROR from a prior post-process).
        self._record_item.set_sensitive(True)
        # Update the indicator's tooltip-equivalent title so panels that show
        # it on hover convey the same status as the menu.
        self.indicator.set_title(f"meetcap — {label}")
        # Show the "Retry now" entry only when at least one meeting is queued.
        retry_visible = state == TrayState.RETRY_PENDING and self._on_retry_now is not None
        self._retry_now_item.set_sensitive(retry_visible)
        if retry_visible:
            self._retry_now_item.show()
        else:
            self._retry_now_item.hide()
        if state in BLINK_STATES:
            self._start_blink()
        return False  # idle_add: don't repeat

    def _start_blink(self) -> None:
        if self._blink_id is not None:
            return
        self._blink_alt = False
        self._blink_id = GLib.timeout_add(BLINK_INTERVAL_MS, self._tick_blink)

    def _stop_blink(self) -> None:
        if self._blink_id is not None:
            GLib.source_remove(self._blink_id)
            self._blink_id = None
            self._blink_alt = False

    def _tick_blink(self) -> bool:
        if self._state not in BLINK_STATES:
            self._blink_id = None
            return False
        self._blink_alt = not self._blink_alt
        icon = ICON_ALT_PATHS.get(self._state) if self._blink_alt else ICON_PATHS[self._state]
        if icon is None:
            icon = ICON_PATHS[self._state]
        self.indicator.set_icon_full(icon, self._state.value)
        return True  # keep ticking

    def _tick_retry_status(self) -> bool:
        """Refresh the State menu label with a live countdown while RETRY_PENDING."""
        if self._state == TrayState.RETRY_PENDING and self._retry_status_provider is not None:
            text = self._retry_status_provider()
            if text:
                self._state_item.set_label(f"State: Retry pending · {text}")
                self.indicator.set_title(f"meetcap — Retry pending · {text}")
        return True  # keep ticking

    def _handle_retry_now(self, _item: Gtk.MenuItem) -> None:
        if self._on_retry_now is not None:
            self._on_retry_now()

    def _handle_open_merge(self, _item: Gtk.MenuItem) -> None:
        if self._on_open_merge_dialog is not None:
            self._on_open_merge_dialog()

    def _refresh_recent(self) -> bool:
        for child in self._recent_submenu.get_children():
            self._recent_submenu.remove(child)
        dirs = sorted(
            (p for p in RECORDINGS_ROOT.glob("*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if not dirs:
            empty = Gtk.MenuItem.new_with_label("(no recordings yet)")
            empty.set_sensitive(False)
            self._recent_submenu.append(empty)
        else:
            for d in dirs:
                item = Gtk.MenuItem.new_with_label(d.name)
                item.connect("activate", lambda _, path=d: self._open_meeting(path))
                self._recent_submenu.append(item)
        self._recent_submenu.append(Gtk.SeparatorMenuItem())
        # Re-parent the cached merge item — must be removed from any prior
        # parent before being appended again or GTK warns about double-add.
        parent = self._merge_dialog_item.get_parent()
        if parent is not None:
            parent.remove(self._merge_dialog_item)
        self._recent_submenu.append(self._merge_dialog_item)
        self._recent_submenu.show_all()
        return False

    def _open_meeting(self, meeting_dir: Path) -> None:
        # Prefer opening the Drive folder if we uploaded; else open the local dir.
        state_file = meeting_dir / ".drive-upload.json"
        if state_file.is_file():
            try:
                import json

                folder_id = json.loads(state_file.read_text()).get("folder_id")
                if folder_id:
                    _xdg_open(f"https://drive.google.com/drive/folders/{folder_id}")
                    return
            except (OSError, ValueError):
                pass
        _xdg_open(str(meeting_dir))

    def _handle_manual_toggle(self, _item: Gtk.MenuItem) -> None:
        self._on_manual_toggle()

    def _handle_auto_toggle(self, item: Gtk.CheckMenuItem) -> None:
        self._on_auto_toggle(item.get_active())


def run_gtk_main_loop() -> None:
    Gtk.main()


def quit_gtk_main_loop() -> None:
    # Safe to call from any thread; GLib.idle_add queues onto the main loop.
    GLib.idle_add(Gtk.main_quit)


# Re-export for callers that don't want to import threading themselves.
__all__ = [
    "TrayIndicator",
    "TrayState",
    "run_gtk_main_loop",
    "quit_gtk_main_loop",
]

_LOCK = threading.Lock()  # reserved for future cross-thread state coordination
_ = _LOCK  # silence unused-warning while leaving a hook
os.environ.setdefault("PYTHONUNBUFFERED", "1")
