# meetcap

Linux app that records meetings on your machine (Zoom, Google Meet in Chrome, Slack Huddle), transcribes them with speaker labels, summarizes, and uploads the bundle to Google Drive. Capture is purely local off PipeWire (mic + sink monitor) — no bot ever joins the call. Transcription and summary each have a local-GPU backend (default: faster-whisper + pyannote, summary via Ollama) and a cloud backend (OpenAI), switchable per stage from the tray.

This file is the durable architecture reference. README is the install/use guide. The code is the source of truth — when in doubt about specifics (exact ffmpeg flags, API call shape, dialog layout), read the named file rather than relying on snippets here.

## Non-goals

- No SaaS, no multi-tenant. One user, one machine, one checkout.
- No meeting bot, no per-app screen scraping, no SDK integration. Audio comes from PipeWire.
- No Windows / macOS. Ubuntu 24.04 + PipeWire 1.0+ only. Wayland window-title lookup not wired (X11 via `xdotool`).
- **No provider abstraction layer.** Cloud path uses OpenAI directly; local path uses faster-whisper / pyannote / Ollama directly. Adding a backend means forking the relevant module, not building a `TranscriberBase`.

## Flow

```
tray (AppIndicator3) polls pw-dump every 2 s
  ├─ Stream/Input/Audio from Zoom/Chrome/Slack? → spawn Recorder
  │    ffmpeg, 2 × `-f pulse`, → 16 kHz 2-ch FLAC (L = mic, R = system)
  │    wpctl boosts mic to mic_boost_to; xdotool grabs window title for slug
  └─ stream gone > 10 s (or ffmpeg died on its own) → stop, spawn
       `meetcap transcribe --upload` as a child subprocess
         mixdown to mono Opus → local pipeline OR OpenAI diarize
         → render transcript.{md,json} + metadata.json
         → optional summary post-pass (Ollama default, or gpt-5.4-mini)
         → upload to Drive folder + notify-send
         failure → RetryQueue arms a retry on 1 / 2 / 5 min cycling backoff
```

## Audio capture

Single ffmpeg with two `-f pulse` inputs (default mic + default sink monitor), merged into a 2-channel FLAC at 16 kHz. L = mic, R = system audio — pre-DAC tap on the system side avoids speaker → mic acoustic bleed for the remote participant's voice. FLAC is the archive; a mono Opus mixdown (`audio.mono.ogg`, 24 kbps) is what gets uploaded to the API.

Load-bearing details (paid for in debugging time) — see `meetcap/audio/recorder.py`:

- **`-thread_queue_size 1024`** on each pulse input. The default of 8 fills under real meeting load and stalls one input while the other waits, risking dropped frames.
- **Per-input `aformat=channel_layouts=mono`** before `amerge`. pipewire-pulse delivers stereo to ffmpeg even with `-ac 1`; without the explicit downmix `amerge` produces an ambiguous channel mapping. Forcing mono per input guarantees the output is exactly L = mic, R = system.
- **`.ogg` extension** on the Opus mixdown, not `.opus`. OpenAI rejects bare `.opus` with HTTP 400 even though it accepts the Opus codec.

Default source resolution (`meetcap/audio/sources.py`): `pw-dump` → the metadata node with `metadata.name == "default"` → `default.audio.sink` and `default.audio.source` (currently active, not `default.configured.*` which is user preference and may be offline). `wpctl status` fallback. `[audio] mic_source` / `sink_monitor` in `config.toml` overrides.

Mic auto-boost (`meetcap/audio/volume.py`): `wpctl set-volume` raises the mic to `audio.mic_boost_to` (default 2.0 = 200%) **only if currently below**. Never lowers. Restored on stop. Plasma UI caps at 1.5; pulse-on-pipewire ceiling is ~3.0.

SIGINT propagates to the whole process group when `meetcap record` is foregrounded; ffmpeg flushes the FLAC trailer and exits 130, treated as success by `OK_EXIT_CODES`.

The watch loop polls `Recorder.is_alive()` each tick. If ffmpeg dies on its own (PulseAudio reset, OOM, …) the daemon detects it within 2 s, queues what was captured for post-process, fires a critical libnotify, and falls through so a fresh recording can start on the next tick if the meeting app is still streaming.

## Auto-detect (`meetcap/detect/pipewire_streams.py`)

Polls `pw-dump` every 2 s for `media.class == "Stream/Input/Audio"` whose `application.name` matches the whitelist (case-insensitive substring): `zoom / google chrome / chrome / chromium / slack / firefox`. Whitelist extension is a one-line edit.

Match appears → spawn Recorder, slug from `xdotool getactivewindow getwindowname` (or `"meeting"`). Match gone > 10 s → stop, spawn `meetcap transcribe --upload` as a subprocess so the watcher returns to polling immediately and back-to-back meetings aren't missed.

## Transcription pipeline

Both backends produce the same segment shape: `[{start, end, text, speaker}]`. Render / summary / upload don't care which one ran.

**Cloud (`backend = "openai"`)** — `meetcap/transcribe/openai_diarize.py`. Single `client.audio.transcriptions.create` call with `model="gpt-4o-transcribe-diarize"`, `response_format="diarized_json"`.

- `chunking_strategy="auto"` is **mandatory for inputs > 30 s**.
- `known_speaker_names` / `known_speaker_references` arrays capped at **4** server-side (API limit).
- Reference clips 2–10 s; format detected from the data-URL MIME type.
- No default `language=` — auto-detect handles mixed en/ru/he meetings. Override via `transcription.language`.

Enrolled names come back lowercased exactly as supplied; unenrolled speakers as capital letters in appearance order.

**Local (`backend = "local"`)** — `meetcap/transcribe/local_pipeline.py`:

1. `faster-whisper` (`large-v3` default, fp16) with `vad_filter=True`, `condition_on_previous_text=True`. ~3 min / hr on RTX 4080.
2. `pyannote/speaker-diarization-community-1` — **gated**: needs `HF_TOKEN` plus EULA acceptance on this model **and** on `pyannote/segmentation-3.0`.
3. SpeechBrain ECAPA-TDNN (`speechbrain/spkrec-ecapa-voxceleb`) — 192-d embedding cosine-match @ threshold 0.30 against enrolled WAVs. Unmatched clusters renumbered A / B / C in appearance order so output looks identical to the cloud path.

VRAM peaks ~5–7 GB at fp16, ~3 GB at int8_float16. Cache under `~/.cache/huggingface/` (~3.3 GB first run). Install via `uv sync --extra local`.

**Render** (`meetcap/transcribe/render.py`): three files into the meeting dir.

- `transcript.json` — segments array verbatim + top-level `model`, `duration`, `text`, `usage`.
- `transcript.md` — speaker-grouped with `[HH:MM:SS]` headers per turn.
- `metadata.json` — `{start_iso, end_iso, duration_sec, model, speaker_count, speakers, cost_estimate_usd, usage}`.

**Summary post-pass** — `meetcap/transcribe/summary.py` (OpenAI) or `summary_ollama.py`. System prompt demands the exact `### 5-bullet summary` + `### Action items` Markdown shape. Result appended to `transcript.md` under `## Summary`. Disable via `--no-summary` or `[transcription] summary_enabled = false`.

`meetcap transcribe` is idempotent: skips the transcribe step if `transcript.json` exists unless `--force`, but **still runs upload** if `--upload` was passed (fixed gotcha — the old early-exit short-circuited upload too). On failure writes `<dir>/.transcribe-error.log` and exits non-zero.

## Retry queue (`meetcap/tray/retry.py`)

The tray-side post-process runs `meetcap transcribe --upload` as a subprocess with a **10 min hard timeout** (`POST_PROCESS_TIMEOUT`). Non-zero exit → daemon writes `<dir>/.retry-state.json` (`attempts`, `next_attempt_at`, `last_error`, `stage`) and arms a retry on a cycling **1 min → 2 min → 5 min → 1 min …** backoff. Loops forever — meetings never give up.

Second daemon thread (`run_retry_thread`) wakes every 10 s and re-runs the post-process for any due entry. Single `_post_process_lock` serializes everything across initial post-process and retries, so concurrent meetings can't fight over the OpenAI key or Drive token.

On tray startup, `RetryQueue.scan()`:

- Reloads `.retry-state.json` entries.
- Seeds entries for orphaned recordings — `audio.flac` present, `transcript.json` absent, `audio.flac` mtime > 60 s old (idle threshold avoids fighting an active ffmpeg).
- Skips dirs marked `.merged-into.json` (see *Multi-segment splice*).

State transitions: success → IDLE (or RETRY_PENDING if other meetings still queued). Failure → RETRY_PENDING (never ERROR — ERROR is reserved for non-recoverable local issues like audio source resolution). State menu shows live countdown updated every 5 s. "Retry pending now" menu item appears whenever the queue is non-empty.

**Abandonment** (anti-infinite-loop). The queue stops retrying when either:
- the last-error message matches a `PERMANENT_ERROR_PATTERNS` substring (HTTP 400 `invalid_request_error`, OpenAI's `audio duration X is longer than Y`, auth failures, `insufficient_quota`, missing audio.flac, etc.) — abandoned on the very first such failure, so we don't waste a single extra API call;
- or `attempts >= MAX_ATTEMPTS_BEFORE_ABANDON` (default 20) — backstop for non-pattern-matched persistent failures (e.g. repeated 10-min timeouts).

Abandoned entries persist on disk with `"abandoned": true` and are skipped by `due()` / `next_due_in()` but still surface in `pending()` so the State line can show `"2 abandoned"`. `force_due_now()` un-abandons every entry — the user's manual escape hatch after they fix the underlying problem (top up credits, switch backend, split the audio, …).

Mid-recording libnotify popups are suppressed (`if … and not self._recording_active`). The tray icon still conveys queue state passively (amber dot for RETRY_PENDING).

## Multi-segment splice (`meetcap merge`)

A meeting can land in N adjacent dirs when ffmpeg dies mid-call, the tray restarts, or the meeting-app's mic stream blinks past the 10 s auto-stop. `meetcap merge DIR DIR…` (CLI) and "Merge meetings…" dialog (`meetcap/tray/merge_dialog.py`, under Recent meetings ▶) concat sources losslessly via ffmpeg's concat demuxer + `-c copy` — works because every recording is the same 16 kHz 2-ch FLAC.

Sources sorted chronologically by the timestamp embedded in `YYYY-MM-DD_HHMM_<slug>`. Output dir: `<earliest_ts>_<slug>-merged/`. Markers written **before** concat so the retry queue stops trying to transcribe sources the moment a merge is committed (clean rollback = delete the markers):

- Each source gets `.merged-into.json` → `{"merged_dir": "<basename>"}`.
- Merged dir gets `.merged-from.json` → `{"sources": [...]}`.

`RetryQueue.scan` / `.add` honour `.merged-into.json`. `meetcap transcribe` early-exits 0 on marked sources — so an in-flight retry started just before the merge resolves cleanly without duplicating work.

Tray flow: MERGING state (orange, blinking, distinct icon) under `_post_process_lock` → concat subprocess (10 min timeout) → on success chain `_post_process` for the merged dir (TRANSCRIBING → UPLOADING → IDLE). Raw concat — no silence padding for gaps.

## Voice enrollment

`meetcap enroll [--name NAME]` records 7 s mono WAV from the default mic, plays it back via `ffplay`, prompts `keep this sample? (y/N/r=retry)`. Saved as `~/.config/meetcap/<name>-voice-sample.wav` (chmod 0600). Up to 4 samples on cloud (API cap); local has no hard cap but accuracy plateaus after 4–5.

## Google Drive

Scope: `https://www.googleapis.com/auth/drive.file` — the only OAuth scope permitted for desktop apps without Google's verification. Per-file: meetcap can only see what it itself created.

`meetcap auth google` runs `InstalledAppFlow.from_client_secrets_file(... SCOPES).run_local_server(port=0)`, writes `~/.config/meetcap/google-token.json` (chmod 0600). Subsequent invocations refresh via `Credentials.from_authorized_user_file`, fall back to the full flow if no refresh token.

Drive layout under `drive.parent_folder_id`: `<meeting_dir>/` containing `transcript` (Google Doc converted from Markdown — full-text searchable in Drive), `transcript.md`, `transcript.json`, `audio.flac`, `metadata.json`. Idempotent — `<dir>/.drive-upload.json` records folder_id + per-file ids; re-running skips already-uploaded files.

## Tray UI (`meetcap tray`)

AppIndicator3 via PyGObject **pinned `>=3.46,<3.51`** — Ubuntu 24.04 ships girepository-1.0; PyGObject 3.51+ needs the 2.0 ABI which isn't packaged yet. Custom monochrome SVG icons under `meetcap/assets/`:

- `idle` (grey), `recording` (red), `merging` / `transcribing` / `uploading` (orange, blink via 2-frame fade @ 500 ms — `*-alt.svg` companions), `pending` (amber dot, static), `error` (red ✗).

Watch + retry loops are background threads; the indicator marshals via `GLib.idle_add` (GTK is not thread-safe). Every Settings-menu choice persists to `~/.config/meetcap/config.toml` via `tomli_w.dump`. The user-facing menu structure lives in README.

`toggle_recording` (was `toggle_manual_recording`) stops the active capture regardless of whether it was manual or auto — auto-stop fires via `_stop_auto_event` checked in the watch loop. Manual record button stays enabled in every state so the user is never trapped by a stale error.

## Storage / config / secrets

| What                       | Where                                                       |
| -------------------------- | ----------------------------------------------------------- |
| Recordings                 | `~/Recordings/meetcap/<YYYY-MM-DD_HHMM>_<slug>/`            |
| App config                 | `~/.config/meetcap/config.toml`                             |
| OpenAI / HF tokens         | `<repo>/.env` (gitignored)                                  |
| Google client secret       | `~/.config/meetcap/google-client-secret.json` (chmod 0600)  |
| Google OAuth token         | `~/.config/meetcap/google-token.json` (chmod 0600)          |
| Voice samples              | `~/.config/meetcap/<name>-voice-sample.wav` (chmod 0600)    |
| systemd user unit          | `~/.config/systemd/user/meetcap.service`                    |
| Whisper / pyannote cache   | `~/.cache/huggingface/`                                     |
| Ollama models              | `~/.ollama/models/`                                         |

Secrets must never be logged. Voice-sample base64 strings in `extra_body` are redacted to `<data:audio/wav;base64,...{N} bytes>` in any debug output.

## Coding + dev

- Python 3.12 + `uv` (lockfile committed). No `pip`, no `poetry`.
- `typer` CLI, `rich` output, `pydantic-settings` for env + TOML, `tomllib` (stdlib) for read, `tomli_w` for write.
- `ruff check` + `ruff format` + `mypy --strict` clean on every commit.
- No comments unless the WHY is non-obvious. No premature abstraction. Secrets live in `<repo>/.env` and `~/.config/meetcap/` — never elsewhere.

```bash
uv sync                                  # base deps
uv sync --extra local                    # + torch/faster-whisper/pyannote/speechbrain
uv run pre-commit install                # or run pre-commit run --all-files manually
uv run ruff check . && uv run ruff format --check .
uv run mypy meetcap
uv run pytest -q
```

Pre-commit gates: `gitleaks` (secret scanning), `ruff` (lint + format), `pre-commit-hooks` (trailing whitespace, EOF, large files, private keys). Tests cover the deterministic / non-IO modules with mocks for subprocess + network — heavy model loads aren't exercised in CI.
