# meetcap

Personal Linux meeting recorder + transcriber + uploader. No bot joins the call — capture is purely local off PipeWire (your mic + the system audio your speakers receive). Transcribes with speaker labels, writes a 5-bullet summary, mirrors the bundle to Google Drive.

- **Tray app** with one-click recording or auto-detect when Zoom / Google Meet / Slack Huddle starts capturing the mic.
- **Local-first by default**: on-device `faster-whisper` + `pyannote` for transcription, Ollama for summary — audio never leaves the box and cost is $0. Cloud backends (`gpt-4o-transcribe-diarize` + `gpt-5.4-mini`) are one click away in the tray menu when you want them.
- **Self-healing**: failed transcribe/upload steps retry on a 1 / 2 / 5 min cycle until they succeed. Multi-segment recordings (ffmpeg restarts, app crashes) can be spliced losslessly via a tray dialog.

Single user, single machine. Ubuntu 24.04 + PipeWire 1.0+. Architecture details in [CLAUDE.md](./CLAUDE.md).

## Quick start

```bash
sudo apt install ffmpeg xdotool libnotify-bin \
                 python3-gi gir1.2-ayatanaappindicator3-0.1
git clone <repo> && cd meetcap
uv sync --extra local                          # base + on-device whisper/pyannote/speechbrain

# Hugging Face token for pyannote (accept the EULA at
# huggingface.co/pyannote/speaker-diarization-community-1 first, then:)
cp .env.example .env && $EDITOR .env           # HF_TOKEN=hf_...
                                               # OPENAI_API_KEY=sk-...   (optional, only if you flip the tray to cloud)

# Local summary via Ollama (skip if you intend to use cloud summaries)
ollama pull llama3.1:8b

# Google Drive OAuth (one-time browser flow)
mkdir -p ~/.config/meetcap
mv ~/.config/client_secret_*.json ~/.config/meetcap/google-client-secret.json
chmod 600 ~/.config/meetcap/google-client-secret.json
uv run meetcap auth google

# Voice sample for speaker labels
uv run meetcap enroll

# Tray launcher + autostart at login
uv run meetcap install-desktop
uv run meetcap install-service
systemctl --user enable --now meetcap.service
```

Click the mic icon in your system tray → **● Start recording** — or just join a Zoom call and it'll record on its own. When the meeting ends, the transcript + summary + audio land in `~/Recordings/meetcap/<dir>/` and in your Drive folder.

**Tested on**: Ubuntu 24.04.4 LTS, KDE Plasma 6 (X11), PipeWire 1.0.5, Python 3.12.6, ffmpeg 6.x, RTX 4080 Laptop (optional local backend). Should work on any modern Linux with PipeWire + Python 3.12. Wayland needs a different window-title lookup than `xdotool` — easy to add if anyone needs it.

## Tray menu

```
State: idle / recording 0:23 / transcribing… / retry pending · 2 pending · next in 1:23
─
● Start / ■ Stop recording                works for manual AND auto recordings
Auto-detect ✓                             pause / resume the watcher
─
Recent meetings ▶
  ┃ <last 5 meetings, click opens Drive folder or local dir>
  ┃ Merge meetings…                       splice multi-segment recordings
Open recordings folder
─
Settings ▶
  Backend ▶                               transcription
    gpt-4o-transcribe-diarize             (cloud, with diarize)
    ● Local (faster-whisper) ▶            large-v3 / -turbo / medium / small
  Summary ▶                               (or off)
    gpt-5.4-mini / gpt-5.5 / gpt-5.4-nano      (cloud)
    ● Local (Ollama) ▶                    discovered from `ollama list`
  Language ▶                              Auto / English / Hebrew / Russian
Enroll voice…
Open config…
─
Quit
```

Every choice persists to `~/.config/meetcap/config.toml` so it survives restarts. Defaults run fully offline (local whisper + Ollama); flip either stage to cloud per meeting from the menu.

## Configuration

`~/.config/meetcap/config.toml` (auto-seeded from `config.toml.example` on first "Open config…" click). The knobs that matter day-to-day:

| Key                                     | What it does                                                                |
| --------------------------------------- | --------------------------------------------------------------------------- |
| `audio.mic_boost_to`                    | PipeWire mic level on record start (default `2.0` = 200%). **Only raises.** |
| `transcription.backend`                 | `"local"` (default) or `"openai"`                                           |
| `transcription.summary_provider`        | `"ollama"` (default) or `"openai"`                                          |
| `transcription.summary_model`           | Ollama tag like `llama3.1:8b` (default), or `gpt-5.4-mini` for cloud        |
| `transcription.language`                | `null` (auto), `"en"`, `"he"`, `"ru"`, ...                                  |
| `retention.delete_local_after_upload`   | default `false` — delete the local dir after Drive upload succeeds          |
| `notifications.enabled`                 | desktop popups on post-process completion                                   |

## Local backend (default, NVIDIA GPU)

Transcription + diarization run on your own GPU and the summary runs via Ollama — audio never leaves the box and cost is $0.

```bash
uv sync --extra local                    # adds torch, faster-whisper, pyannote, speechbrain
echo "HF_TOKEN=hf_xxx" >> .env           # accept the EULAs first:
                                         #   huggingface.co/pyannote/speaker-diarization-community-1
                                         #   huggingface.co/pyannote/segmentation-3.0
ollama pull llama3.1:8b                  # default summary model (5 GB, fits in 12 GB VRAM)
```

First transcribe call downloads ~3.3 GB to `~/.cache/huggingface/`. On an RTX 4080 a 1-hour meeting runs in ~3–5 min total (transcribe ~3 min + diarize ~1–2 min + speaker match <10 s). VRAM peaks ~5–7 GB at fp16; set `transcription.local_compute_type = "int8_float16"` to halve it.

Want a different summary model? `Settings ▶ Summary ▶ Local (Ollama)` lists everything from `ollama list`. Want to flip a stage to cloud? `Settings ▶ Backend ▶ gpt-4o-transcribe-diarize` and/or `Settings ▶ Summary ▶ gpt-5.4-mini`.

## Headless / CLI usage

```bash
uv run meetcap record --slug "1on1-with-alice"             # Ctrl-C to stop
uv run meetcap watch                                       # CLI-only auto-detect (no tray)
uv run meetcap transcribe ~/Recordings/meetcap/<dir>
uv run meetcap upload     ~/Recordings/meetcap/<dir>
uv run meetcap transcribe ~/Recordings/meetcap/<dir> --upload
uv run meetcap merge      ~/Recordings/meetcap/<dirA> ~/Recordings/meetcap/<dirB>
```

Recordings land in `~/Recordings/meetcap/<YYYY-MM-DD_HHMM>_<slug>/` as `audio.flac` (2-channel: L = mic, R = system audio) plus the post-process artifacts (`audio.mono.ogg`, `transcript.{json,md}`, `metadata.json`).

## Cost

Rough monthly estimate for **3 × 1 hr meetings / week** (≈ 780 min):

| Pipeline                                                 | Total / month |
| -------------------------------------------------------- | ------------: |
| Fully offline — local transcribe + Ollama summary (default) |  **≈ $0**  |
| Hybrid — local transcribe + `gpt-5.4-mini` summary       |   **≈ $0.14** |
| Cloud everything                                         |   **≈ $5–10** |

`metadata.json` per meeting records a `cost_estimate_usd` from `transcription.cost_per_minute_usd`. Adjust after a few real meetings to match your actual OpenAI billing.

## Troubleshooting

- **Tray icon doesn't appear.** Run `uv run meetcap tray` from a terminal — early errors print to stdout. KDE Plasma occasionally hides AppIndicators until `plasmashell` is restarted.
- **`meetcap auth google` says "client secret missing".** Move your OAuth desktop-client JSON to `~/.config/meetcap/google-client-secret.json` (chmod 600).
- **OAuth says "Google hasn't verified this app".** Expected for your own personal client. Click *Advanced → Go to … (unsafe)*.
- **Zoom / Teams hear you too quietly.** Bump `audio.mic_boost_to` to `2.5`–`3.0`.
- **"No meeting detected" with Zoom open but idle.** Detection fires on PipeWire mic capture, not window presence — start the call first.
- **Post-process failure popup.** Open `<meeting_dir>/.post-process.log` for full stdout+stderr. The retry queue will keep trying every 1 / 2 / 5 min, so transient failures usually resolve themselves.
- **`OpenAI: Unsupported file format opus`.** Already worked around — meetcap names the mixdown `audio.mono.ogg` because the API rejects bare `.opus`.

## Privacy & secrets

- `.env`, OAuth tokens, voice samples, and the Google client secret are gitignored and live with `chmod 0600`. Verify with `git check-ignore -v .env`.
- Every commit is gated by [gitleaks](https://github.com/gitleaks/gitleaks) (see `.pre-commit-config.yaml`). To activate after cloning:

  ```bash
  uv run pre-commit install                # wires up .git/hooks/pre-commit
  uv run pre-commit run --all-files        # one-time full-repo scan
  ```

  If `pre-commit install` refuses because of a global `core.hooksPath`, override per-repo with `git config --local core.hooksPath .git/hooks && uv run pre-commit install`, or just run `uv run pre-commit run --all-files` before each push.
- **Default pipeline (local backend + Ollama summary)** ⇒ no audio, transcript, or summary touches the network. Only the optional Drive upload does, and it uses the `drive.file` scope so meetcap can only see files it itself created.

## Project layout

```
meetcap/
  cli.py              typer subcommands (record, watch, tray, transcribe, upload,
                      merge, enroll, auth google, install-{service,desktop})
  config.py           pydantic-settings + tomllib + TOML writer
  audio/              PipeWire source resolution, ffmpeg recorder, mic volume boost
  detect/             mic-capturing app + active-window detection
  transcribe/         mixdown, OpenAI diarize, local whisper / pyannote / ECAPA,
                      cloud + Ollama summary, render
  drive/              OAuth, folder/file/Google-Doc upload, idempotent state
  merge/              multi-segment concat plan + ffmpeg splice
  voice/              enrollment (7 s WAV samples)
  tray/               AppIndicator3 indicator + daemon + state machine +
                      retry queue + merge dialog
  notify.py           libnotify wrapper
  assets/             tray SVG icons
  watch.py            headless CLI watcher
systemd/meetcap.service   user unit (autostarts `meetcap tray`)
tests/                pytest unit tests for the deterministic modules
```

## Contributing

```bash
uv sync
uv run pre-commit install                # or run manually below
uv run pre-commit run --all-files
uv run pytest -q
uv run mypy meetcap
```

One feature per PR. Architecture decisions go in `CLAUDE.md`. Issues + PRs welcome.

## License

[MIT](./LICENSE).
