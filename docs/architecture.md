# meetcap architecture

## Goals and constraints

| Need | How it is met |
|---|---|
| Record every meeting even when Teams recording is off or blocked | Capture happens on the Mac at the OS level (ScreenCaptureKit through OBS), independent of the meeting app |
| No manual steps per meeting | Mic-ownership detection starts and stops recording automatically |
| Don't fill the laptop | OBS writes to a temporary folder. The recording is moved into storage (a hard link on the same disk) or uploaded to Azure, and the temporary copy is deleted once every stored file is re-checked by size. Audio for transcription is ~14 MB/h |
| Works with no accounts, and with the one you have | Storage: a local folder (default) or Azure Blob. Transcripts: Whisper on the Mac, Groq, OpenRouter, OpenAI, Azure OpenAI, Azure Speech or off. Notes: OpenRouter, Groq, Claude, OpenAI, Azure OpenAI, Ollama or off |
| Capture what was shown, not only what was said | Screen video at 10 fps, plus scene-change screenshots sent to the notes model |
| Free / open source locally | OBS (GPL), ffmpeg, a small Swift helper, and Python with MIT/Apache deps |
| Survive crashes, sleep and offline | Hybrid MP4 (crash-safe container), SQLite job state, per-stage retries, orphan scan at startup |

## Layers

```
Presentation   menubar.py (rumps)   cli.py (argparse)   web/setup.html   web/index.html    <- thin; no logic
SDK facade     agent.MeetCapAgent   pipeline.Pipeline   setupui.SetupSession   health.*     <- what the UIs call
Services       detect.MeetingDetector   jobs.JobStore   render.*   reader.*   library.MeetingLibrary
Factory        providers.make_store / make_transcriber / make_summarizer   (PRESETS: one table per service)
Adapters       storage.LocalStore | AzureBlobStore
               transcribe.LocalWhisperTranscriber | OpenAICompatTranscriber (Groq, OpenRouter, OpenAI, Azure OpenAI)
                          | AzureFastTranscriber (Speech)
               summarize.ChatSummarizer (OpenRouter, Groq, OpenAI, Ollama, Azure OpenAI) | AnthropicSummarizer
               detect.MicWatch   recorder.ObsRecorder   media.* (ffmpeg)
External       micwatch (Swift, Core Audio)   OBS + obs-websocket v5   the services you picked
```

Each adapter implements a small Protocol (`MicSource`, `Recorder`, `BlobStore`, `Transcriber`, `Summarizer`). The agent builds them through `providers.py`, and the pipeline takes them as constructor arguments, so tests inject fakes. Everything provider-specific lives in `providers.PRESETS`: base URL, default model, Keychain entry, request limits. The setup screen, `meetcap doctor` and the pipeline all read that one table. A transcriber or summarizer of `None` means that role is off, and the pipeline still finishes the meeting.

## Detection

`micwatch` reads `kAudioHardwarePropertyProcessObjectList`, and for each process `kAudioProcessPropertyIsRunningInput`, `…BundleID` and `…PID`. These are macOS 14+ APIs. Reading them opens no device and touches no audio data. On macOS 26, property listeners on `IsRunningInput` register but never fire, so the agent polls every 2 s. The cost is negligible.

`MeetingDetector` is a pure state machine: start after 3 s of continuous mic use by a listed app, stop after 20 s without. Bundle matching is by prefix, so `com.microsoft.teams2.helper` counts as Teams. Browsers are opt-in, because any website using the mic would otherwise trigger a recording.

Edge cases handled:

- **Manual stop during a call.** Auto-record is suppressed until the mic is released, so it doesn't restart two seconds later.
- **Failed start.** For example, a permission was revoked. You get a notification, and the agent retries every 30 s while the call continues.
- **Very long call.** The recording rolls over into a new file after `max_meeting_hours`.
- **meetcap crashes mid-recording.** On restart it adopts OBS's in-flight recording and queues any orphaned files in the output folder.

## Recording

OBS 30+ on macOS captures video and system audio through ScreenCaptureKit and the mic through Core Audio. That means no BlackHole or Multi-Output device, and volume keys keep working. `meetcap setup-obs` sets up:

- A **MeetCap** profile: Simple output, `hybrid_mp4`, Apple H.264, "Small" quality, 10 fps, output capped at 1920 wide.
- A **MeetCap** scene with three sources:
  - `MeetCap Screen` (`screen_capture`, the chosen display). Its own audio is muted so video can be turned off without losing sound.
  - `MeetCap System Audio` (`sck_audio_capture`, desktop).
  - `MeetCap Mic` (`coreaudio_input_capture`, follows the system default). It is skipped if OBS already has a global mic.
- obs-websocket on 127.0.0.1:4455 with auth. The password is stored in the Keychain.

It also sets the file name pattern to `meetcap_%CCYY-%MM-%DD_%hh-%mm-%ss`. The orphan scan only adopts files with that prefix, so personal videos in the same folder are never uploaded or deleted.

**OBS lifecycle.** By default OBS runs only while a meeting is being recorded.

1. meetcap opens it hidden (`open -g … --minimize-to-tray --multi`) when a meeting is detected.
2. Once the file has settled and OBS reports it isn't recording, meetcap ends the OBS process directly (`close_after_recording`). OBS 32.2's normal quit crashes in its Core Audio mic input on macOS 26 (every time on the M3), and at that point the file is complete and there's nothing left to save. Setup still uses the normal quit (Apple Events, then SIGTERM), because OBS writes its settings at exit.

Keeping OBS warm (`keep_obs_running: true`) removes the 5-10 s start delay. The cost is that the mic and ScreenCaptureKit stay open all day: macOS indicators stay on and Bluetooth headsets stay in call mode.

**Crash dialogs.** After a crash, OBS shows a modal "unclean shutdown / safe mode" dialog before its websocket starts. That would block a headless launch, so the recorder deletes OBS's crash markers before each launch: `.sentinel/run_*` on 32.x and `safe_mode` on 31.x. Single-instance is enforced with `pgrep`.

**Crash blame.** When OBS dies during setup, meetcap reads the crash report macOS writes about 10 seconds later, before it decides what to switch off (`crash_kind`):

- A crash inside obs-websocket or OBS's Core Audio hand-off is OBS's own bug. meetcap reopens OBS and retries without switching any source off.
- Only a crash inside a capture plugin, or one with no readable report, switches off (quarantines) the source that was just touched.

On 28 September 2026 the screen source was switched off wrongly. It was blamed for an obs-websocket crash (`strlen` of a NULL display entry) that happened while meetcap asked OBS for its display list. Display UUIDs now come from CoreGraphics (`display_uuids()`), and meetcap never sends that request.

**Profile drift.** If you opened OBS by hand on another profile, `ensure_ready` switches it back to the MeetCap profile and collection before recording. Otherwise a file could be written with your settings and then deleted.

`StopRecord` returns before the file is finalized. The recorder polls `GetRecordStatus` and the pipeline's first stage waits for the file size to settle.

**Manual OBS fallback** (if `setup-obs` fails on your OBS version):

1. In OBS, create a profile and scene collection named MeetCap.
2. Add sources: macOS Screen Capture (display), macOS Audio Capture (desktop audio), Audio Input Capture (default mic). Mute the Screen Capture source's own audio in the mixer.
3. Settings, Output: Simple, Recording Path `~/Movies/MeetCap`, format Hybrid MP4, encoder Apple VT H264, quality Small.
4. Settings, Video: 10 FPS.
5. Tools, WebSocket Server Settings: enable, set a password, then run `meetcap secret set obs-password`.

## Processing pipeline

One job per recording, with state in SQLite (`~/Library/Application Support/meetcap/jobs.sqlite3`).

- **One worker, in order.** A single worker thread runs jobs in order. It pauses between stages whenever a call is live (`pause_while_recording`), so a 700 MB upload never competes with Teams for bandwidth.
- **Leases.** Each job is claimed with an atomic lease (`lease_until`), so the agent and `meetcap process --now` can never process the same file twice.
- **Video decision.** Whether a recording carries video is fixed when it is made (`job.data.video`). A later config change can't cause a video file to be deleted without being uploaded.
- **Unknown duration.** A recording whose length ffprobe can't read (a crash-truncated file) is always processed, never discarded as "too short".

| # | Stage | Idempotency | Failure |
|---|---|---|---|
| 1 | finalize | read-only checks | Missing file: retry. No audio stream: dead-letter immediately. Auto-recordings under `min_meeting_seconds` are discarded (status `skipped`) |
| 2 | extract_audio | skipped if `audio.ogg` exists | retry |
| 3 | upload_media | skipped per file if the blob exists with the recorded size; checkpoint after each file | retry; local files kept |
| 4 | transcribe | skipped if `transcript.json` exists (written atomically) | 429/5xx/network are transient; 4xx is retried too, because the usual cause is config you then fix |
| 5 | publish_transcript | overwrite | retry |
| 6 | extract_frames | overwrite | non-fatal: continues with no screenshots |
| 7 | summarize | skipped if `notes.json` exists; falls back to text-only if the vision call fails | retry |
| 8 | publish_notes | overwrite | retry |
| 9 | cleanup | re-checks every uploaded blob's size before deleting | mismatch: retry, local files kept |

Retry policy: backoff of `30 s × 2^(n-1)` capped at 1 h, plus 10% jitter. After `max_attempts` (8) on the same stage the job becomes `failed` and you get a notification. `meetcap retry` resets it at that stage. Only failed jobs can be retried. Imported files (`meetcap process`) are never deleted.

Recovery: at startup the agent adopts any recording OBS is still running and queues it. Every 10 minutes while idle, it queues `meetcap_*` files that no job knows about.

**Transcription.**
- Azure Speech uses fast transcription, which is synchronous and takes < 5 h and < 500 MB per request, so audio is split at 4 h.
- The OpenAI-compatible engines chunk audio to their limits:
  - Groq: 20 min, which fits the 25 MB free-tier cap.
  - OpenRouter: 8 min, because its upstream providers time out after 60 s.
  - OpenAI: 10 min.
- Each engine is asked once for the richest response format it accepts, in this order: `diarized_json`, then `verbose_json`, then `json`.
- `azure.speech.phrase_list` (called "names and words" on the setup screen) is passed to every engine that takes a prompt.
- Whisper on the Mac drops segments that fall in measured silence, because Whisper invents text there.

**Notes.**
- The transcript is sent as compact `[HH:MM:SS] S2: text` turns. Models that read images also get up to 24 scene-change screenshots.
- The reply is parsed as JSON. When a model rejects JSON mode, the request is retried without it and the JSON is taken from the text reply.
- meetcap renders the Markdown itself, so the format stays fixed.
- Transcripts longer than the provider's `max_chars` are split. Each part gets its own notes, then a merge pass combines them.
  - The limit is 16k characters on Groq, whose free tier allows 8,000 tokens a minute, and 350k on the large-context services.
  - The OpenAI SDK waits out 429 responses using the `Retry-After` header. Groq and OpenRouter get more retries.
- Speaker names are applied only when the model cites evidence from the call.

## Setup screen

`meetcap setup` starts `setupui.py` on 127.0.0.1 on a free port with a random one-time token.
- The token travels in the URL fragment and comes back in an `X-Meetcap-Token` header, and the `Host` header is checked. So only the tab meetcap opened can save.
- The page describes each section as data (options, fields, the Keychain entry per key) and renders it with one function.
- Keys go from the request straight to the Keychain. `GET /api/state` only says whether each key is set.
- Saves are allowlisted by key and type. Choices are checked against the lists in `config.py`.
- The config is written with ruamel, so comments survive.
- **Set up OBS** runs the same `setup_obs()` as the CLI in a thread, and the page polls its steps.
- **Finish** rebuilds the notes page, restarts the login item and opens the page.

## Notes page from a folder (local storage)

With `storage.backend: local`, the meetings folder carries its own reader. `reader.publish_static()` runs after every meeting and when the agent starts, and writes three things:
- `Meeting Notes.html`: the same `index.html`, with `{"mode": "static"}` and the sprites baked in.
- `meetcap-index.js`: one row per meeting, with the text to search.
- `reader.js` in each meeting folder: that meeting's meta, notes, transcript phrases, screenshot names and media file names.

Browsers won't let a `file://` page fetch JSON beside it, but they do load scripts, so the data ships as scripts. `StaticSource` in the page loads the index, then each meeting's script on demand, and in every transcript when you search. Media use relative URLs, and an AAC copy of the audio (`audio.m4a`) is made because Safari doesn't play Opus. The page works with meetcap stopped, and in a synced folder it works on any Mac that has the folder.

## Notes library (local reader)

`MeetingLibrary` (`library.py`) keeps a local copy of each finished meeting under `~/Library/Application Support/meetcap/library/<id>/`: `meta.json`, `notes.json`, `transcript.json` (phrases as `{t, end, speaker, text}`), `frames/*.jpg`, and `audio.m4a` (AAC 64 kbps, so Safari and Chrome can seek it). The pipeline writes the copy in `publish_notes`, while the files are still on disk. `sync()` fills the gaps from Blob every 10 minutes and on demand: meetings recorded before the library existed, or on another Mac. Audio for synced meetings is fetched and converted on first play, then kept in an LRU cache capped by `ui.library_audio_cache_mb` (1 GB default). Screen recordings are never copied; the reader streams them from Blob in ranges of up to 8 MB.

`webui.py` is a stdlib `ThreadingHTTPServer` that runs inside the agent, so it starts at login with it. Its API is small and read-mostly: list/search, one meeting, audio (HTTP ranges), frames, video, status, and `POST /api/sync`. The page (`web/index.html`) is a single file of vanilla JS with no build step and no third-party requests. It is a thin consumer of that API; search and data shaping live in `MeetingLibrary`.

### Standalone reader (no meetcap process)

`meetcap reader` writes `~/Library/Application Support/meetcap/Meeting Notes.html`. It is the same `index.html` with three additions: a read-only link, the puppy sprites as data URIs, and a Content-Security-Policy meta tag. When the page runs from a file, it talks to Azure itself. It sends one List Blobs call per 5,000 blobs under your prefix, then reads `meta.json` and `notes.json` for each meeting, six at a time. Transcripts load when you open a meeting or search. Audio (`audio.ogg`), video (`recording.mp4`) and screenshots are plain `<audio>`, `<video>` and `<img>` elements pointed at the blobs, so seeking uses Azure's own range support. In the page, `LocalSource` (the 127.0.0.1 API) and `AzureSource` implement the same six calls. Everything above them is shared.

- **The link.** A service SAS with `sp=rl` and `spr=https`, bound to the container's stored access policy `meetcap-reader`. The policy holds the expiry (365 days by default), so `meetcap reader` renews it and the URL doesn't change. `--revoke` deletes the policy, and every copy stops working within about 30 seconds. With `auth: aad` it is a user-delegation SAS, which Azure caps at 7 days. With `auth: sas` it reuses the configured SAS if that allows read and list.
- **Scope.** A container SAS can't be limited to a prefix. If colleagues share the `meetings` container, the link can read their folders too. In that case, give yourself a container of your own.
- **CORS.** A page opened from a file has a `null` origin, so the Blob service needs one CORS rule (origin `*`, GET and HEAD). The command merges it into the existing rules after asking. Reads still need the signature, so the rule grants nothing by itself. One thing does change: any public container in the same account becomes readable by scripts on other sites, though those containers are already readable by anyone.
- **Where the link lives.**
  - The file is written 0600 to a folder iCloud doesn't sync. The bookmark is a `file://` URL with no secret in it.
  - A link pasted into a plain copy of the page stays in that browser's `localStorage`. Chrome shares that storage across every `file://` page, so it's second best to the built file.
- **CSP.** `default-src 'none'`. Scripts and styles are inline only. `connect-src`, `media-src` and `img-src` allow `https://*.blob.core.windows.net` only. There is no referrer.

## Security and data governance

- **Secrets.** Only in the macOS login Keychain (`keyring`, service `meetcap`). Nothing secret is written to config, env files or logs. The preferred path is keyless: `auth: aad` uses `az login` through `DefaultAzureCredential`, with data-plane roles (Storage Blob Data Contributor, Cognitive Services User, Cognitive Services OpenAI User) granted by Terraform.
- **Storage.**
  - Private container; public blob access disabled; TLS 1.2+.
  - Shared-key auth off by default.
  - Optional IP allow-list.
  - 14-day soft delete.
  - Lifecycle tiering and deletion.
- **Data in the blobs.**
  - Voices, screen contents (anything visible, including other apps) and names inferred by the model.
  - Treat it as confidential. Keep it in storage and a region that your contracts allow.
  - Azure OpenAI and Speech in your tenant do not train on your data. Confirm the deployment region matches residency requirements.
- **Consent.** The tool deliberately works without the meeting app's recording banner, so disclosure is on you. See the README.
- **Where content goes.** With Whisper on the Mac and Ollama, nothing leaves the Mac. Otherwise the chosen transcription service receives the audio, and the chosen notes service receives the transcript and, for models that read images, screenshots. Free models on OpenRouter can change, and some of their providers keep what you send.
- **Setup screen.** 127.0.0.1 only, with a one-time token, a Host check and a strict CSP. Keys are never echoed back, and the request body is capped at 64 KB.
- **Notes reader.**
  - Binds to 127.0.0.1 only; nothing on the network can reach it.
  - Rejects any request whose `Host` header isn't `127.0.0.1` or `localhost`. That stops DNS-rebinding pages in your browser from reading it.
  - Sends a strict CSP (`default-src 'self'`, `frame-ancestors 'none'`), `nosniff` and `no-referrer`.
  - The one state-changing call (`POST /api/sync`) requires an `X-Meetcap: 1` header, which a cross-site form cannot set.
  - IDs and frame names are validated against fixed patterns, so paths never come from the request.
  - It is a local copy of confidential meeting data. FileVault covers it at rest, and `uninstall.sh --purge` removes it.
- **Local exposure.** Media sits in `~/Movies/MeetCap` only until upload is verified. FileVault protects it at rest. The job DB holds paths, titles and blob names, never content.

## Operability

- **Logs.** Structured JSON logs in `~/Library/Logs/meetcap/meetcap.log`, rotated at 5 × 5 MB. launchd stdout and stderr go next to it.
- **Health.** `meetcap doctor [--deep]` checks every dependency end to end. The menu bar shows the queue depth and the last error.
- **Runtime.** A LaunchAgent (`com.meetcap.agent`) starts the menu bar app at login and restarts it if it crashes, but not if you quit it. `install.sh` removes meetcap login items left by older installs under other names.
- **Upgrade.** `git pull && ./install.sh` is idempotent.
- **Uninstall.** `./uninstall.sh [--purge]`.

## Options considered

| Option | Why not (as the whole solution) |
|---|---|
| Teams built-in recording + Copilot recap | Depends on the organizer's policy and license. Often disabled for external or client meetings. Nothing outside Teams |
| Meetily (MIT) | Local Whisper notes, audio only. Auto-detect is in the paid tier. No video, no Azure upload |
| Anarlog, formerly Hyprnote/Char (MIT) | Local notepad with no screen video. Its only cloud option is its own sync |
| Screenpipe | License moved to commercial and source-available in June 2026. Stores frames plus accessibility data rather than a meeting recording |
| pasrom/meeting-transcriber (MIT) | Good audio-only alternative with a record-only mode (Core Audio process taps, no Screen Recording permission). No video |
| Native ScreenCaptureKit recorder in Swift | Removes the OBS dependency, but means owning a signed capture binary and its TCC quirks. Worth doing as v2 once the flow is proven |
| Processing in an Azure Function (Blob trigger) | More robust when the laptop sleeps, but adds a Function App, Event Grid and identity to deploy. The pipeline is already stateless per stage so it can move there later |

## Unverified assumptions

These are marked so they are tested first on your machine:

1. `setup-obs` request sequence against your exact OBS build. It was written from obs-websocket 5.x source and the protocol docs, but not run live.
2. Which Teams process holds the mic. Prefix matching covers the main app and helpers; confirm with `meetcap mics` during a call.
3. Teams keeps the input open while you are muted. If not, a long mute longer than 20 s would split the recording. Raise `stop_after_seconds` if you see that.
4. Quitting OBS after each meeting leaves nothing behind that blocks the next launch. Crash markers are cleared either way; if you see a dialog at launch, set `keep_obs_running: true`.

The review pass checked the rest against source and docs: the Swift Core Audio calls, every obs-websocket request and field used, OBS source settings and profile keys, the Speech request format, the OpenAI v1 client, the Blob upload arguments and the Terraform attribute names.
