# meetcap SDK reference

The Python package `meetcap` (Python ≥ 3.10) is the whole product. The menu bar app and the CLI are thin consumers of the classes below. Anything a UI can do, a script or an agent can do with the same calls.

```python
from meetcap import MeetCapAgent, Pipeline, JobStore, load_config
```

## Configuration

```python
from meetcap.config import load_config, Config, problems, ensure_dirs, CONFIG_PATH, STATE_DIR, DB_PATH, WORK_DIR, LOG_DIR

cfg: Config = load_config()          # ~/Library/Application Support/meetcap/config.yaml, defaults if absent
issues: list[str] = problems(cfg)    # settings that stop processing ([] = ready; the defaults need nothing)
ensure_dirs(cfg)                     # state, logs, work, bin, output folders
```

`Config` is a tree of dataclasses. Unknown YAML keys raise `ConfigError`.

| Section | Fields (defaults) |
|---|---|
| `detection` | `enabled=True`, `apps=[teams2, teams, zoom, webex…]`, `browsers=[]`, `poll_seconds=2`, `start_after_seconds=3`, `stop_after_seconds=20`, `min_meeting_seconds=90`, `max_meeting_hours=5`, `micwatch_path=""` |
| `recording` | `video=True`, `fps=10`, `max_width=1920`, `display_index=0`, `output_dir="~/Movies/MeetCap"`, `keep_obs_running=False`, `obs: {host, port=4455, profile, collection, scene="MeetCap", app_path, launch_timeout_seconds=45}` |
| `storage` | `backend="local"` (`local` \| `azure`), `local_path="~/MeetCap"` |
| `transcription` | `engine="local"` (`local` \| `groq` \| `openrouter` \| `openai` \| `azure-openai` \| `azure-speech` \| `none`), `model=""` (provider default), `base_url=""` (openai only), `local_model="mlx-community/whisper-large-v3-turbo"`, `split_speakers="auto"` (`auto` \| `on` \| `off`; auto = on for local) |
| `summary` | `provider="none"` (`none` \| `openrouter` \| `groq` \| `anthropic` \| `openai` \| `azure-openai` \| `ollama`), `model=""`, `base_url=""` (openai / ollama), `max_transcript_chars=0` (provider default), `screenshots=True` |
| `azure.storage` | `account_url`, `container="meetings"`, `prefix=""`, `auth="aad"` |
| `azure.speech` | `endpoint`, `api_version="2025-10-15"`, `locales=["en-US"]`, `max_speakers=8`, `profanity_filter="None"`, `phrase_list=[]`, `auth="key"`, `timeout_seconds=900` |
| `azure.openai` | `endpoint`, `deployment="gpt-4.1"`, `auth="key"`, `max_transcript_chars=350000`, `timeout_seconds=300` |
| `processing` | `pause_while_recording=True`, `delete_local_after_upload=True`, `screen_insights=True`, `max_frames=24`, `notes_dir=""`, `open_notes_when_ready=False`, `user_name=""`, `prompt_file=""`, `max_attempts=8`, `audio_bitrate="32k"` |

Derived: `cfg.output_dir`, `cfg.micwatch_path`, `cfg.notes_dir` (a `Path` or `None`), `cfg.local_root`, `cfg.blob_prefix` (Azure's `prefix`, `""` for local), `cfg.reader_path` (the notes page file).

`load_config` migrates older files: `engine: speech` becomes `azure-speech`, `openai` becomes `azure-openai`, and a config with Azure storage or Azure OpenAI details but no `storage` / `summary` section keeps using them.

Environment overrides, for tests and multiple profiles: `MEETCAP_HOME`, `MEETCAP_CONFIG`, `MEETCAP_LOG_DIR`.

## Authentication and secrets

`auth: aad` uses `azure.identity.DefaultAzureCredential`, which on a laptop means `az login`. The identity needs these roles:

| Service | Role | Scope |
|---|---|---|
| Blob | Storage Blob Data Contributor | storage account |
| Speech | Cognitive Services User | Speech resource (needs a custom subdomain) |
| OpenAI | Cognitive Services OpenAI User | Azure OpenAI resource |

`auth: key` reads the Keychain:

```python
from meetcap.vault import get_secret, set_secret, delete_secret, require, KNOWN_SECRETS
set_secret("groq-key", "...")        # KNOWN_SECRETS: obs-password, speech-key, openai-key (Azure), storage-connection-string,
                                     # storage-sas-url, groq-key, openrouter-key, anthropic-key, openai-api-key
key = require(get_secret, "speech-key")   # raises RuntimeError with the fix command if missing
```

## MeetCapAgent (`meetcap.agent`)

Owns detection, recording and the background worker. It is thread-safe.

```python
agent = MeetCapAgent(cfg,
    store=None,              # JobStore; default DB_PATH
    recorder=None,           # Recorder; default ObsRecorder
    mic=None,                # MicSource; default MicWatch(cfg.micwatch_path)
    pipeline_factory=None,   # () -> Pipeline; default builds Azure adapters from cfg
    notifier=notify,         # (title, message, sound=False) -> None
    clock=time.time)
```

| Method | Returns | Notes |
|---|---|---|
| `start()` | None | Creates folders, recovers in-flight/orphaned recordings, starts detect and worker threads, warms OBS |
| `shutdown()` | None | Stops threads; finalizes an active recording |
| `start_recording(app="manual", manual=True)` | `bool` | False if already recording or OBS refused (then `last_error` is set and you get a notification) |
| `stop_recording(user_initiated=True)` | `Job \| None` | Queues the file. A user stop during a live call suppresses auto-restart until the mic is released |
| `set_auto(enabled)` | None | Toggle detection |
| `tick(now=None)` | None | One detection step; the detect thread calls it every `poll_seconds` |
| `process_due()` | `Job \| None` | Runs the next due job unless a call is live |
| `process_file(path, app="import")` | `Job` | Queue an existing file; it is never deleted |
| `retry_failed()` | `int` | Re-queue every dead-lettered job |
| `recover()` / `scan_orphans(min_age=120)` | `int` | Adopt OBS's in-flight recording; queue unknown media in `output_dir` |
| `status()` | `AgentStatus` | `recording, app, since, manual, auto, detection_available, counts, last_error` |
| `pipeline()` | `Pipeline` | Lazily built; raises if Azure config is incomplete |

`meetcap.agent.setup_obs(cfg) -> list[str]` performs the one-time OBS configuration and returns a human-readable log.

## Pipeline (`meetcap.pipeline`)

```python
p = Pipeline(cfg, store, blob, transcriber, summarizer, notifier=notify, work_root=WORK_DIR, clock=time.time)
job = p.run(job, should_yield=lambda: False)   # runs stages until done / failed / yield
```

Stages (`meetcap.models.Stage`) run in this order: `finalize`, `extract_audio`, `upload_media`, `transcribe`, `publish_transcript`, `extract_frames`, `summarize`, `publish_notes`, `cleanup`, `done`. Each stage is idempotent and checkpointed.

On an exception, `store.fail()` records it and schedules a backoff. `PermanentError` dead-letters the job immediately.

Blob layout under `job.blob_prefix` (`{prefix}/{yyyy}/{mm}/{dd}/{HHMM}_{app}_{id6}`):

- `recording.mp4`, `audio.ogg`
- `transcript.{md,vtt,json}`
- `notes.{md,json}`
- `frames/HH-MM-SS.jpg`
- `meta.json`

## JobStore (`meetcap.jobs`)

SQLite in WAL mode, safe across threads and processes.

| Method | Notes |
|---|---|
| `add(job) -> Job` | Idempotent on `source_path` |
| `save(job)` / `get(id_or_prefix)` / `by_source(path)` | |
| `list(status=None, limit=50)` | Newest first |
| `next_due(now)` | Oldest pending, unleased job whose backoff has elapsed |
| `claim(job, now, ttl=4h) -> bool` / `release(job)` / `claim_next(now)` | Atomic lease so two processes never run one job at once |
| `advance(job, stage)` | Resets attempts; `Stage.DONE` sets status `done` unless it is already `skipped` |
| `fail(job, error, max_attempts, now)` | Backoff `30 s·2^(n-1)`, capped at 1 h, plus jitter; status `failed` at `max_attempts` |
| `retry(job)` | FAILED jobs only (otherwise `ValueError`). Back to `pending`, attempts 0, same stage |
| `counts()` | `{"pending": n, "done": n, "failed": n, "skipped": n}` |

`Job` fields: `id, source_path, started_at, ended_at, app, stage, status, attempts, next_attempt_at, last_error, blob_prefix, data, created_at`. `job.owned` is False for imported files.

## Providers (`meetcap.providers`) and checks (`meetcap.health`)

```python
from meetcap import providers, health
store = providers.make_store(cfg, get_secret)              # LocalStore(cfg.local_root) or AzureBlobStore
stt = providers.make_transcriber(cfg, get_secret)          # None when transcription.engine = none
notes = providers.make_summarizer(cfg, get_secret)         # None when summary.provider = none
providers.PRESETS["groq"]                                   # Preset(label, base_url, secret, stt_model, chat_model, ...)
providers.describe(cfg)                                     # {"storage": ..., "transcription": ..., "notes": ...}

health.secrets_needed(cfg)                                  # Keychain entries the current choices need
ok, message = health.storage(cfg, get_secret)               # also health.transcription / health.summary
```

A missing key raises `RuntimeError("Secret 'groq-key' is missing. Run: meetcap secret set groq-key")`. Health checks return `(True | False | None, message)`, where `None` means that role is off.

## Setup screen (`meetcap.setupui`)

```python
from meetcap.setupui import SetupSession, serve
session = SetupSession(config_path, get=get_secret, put=set_secret, remove=delete_secret)
httpd, url = serve(session)           # url = http://127.0.0.1:<port>/setup#t=<token>
session.save({"transcription.engine": "groq"}, {"groq-key": "..."})   # only EDITABLE keys; secrets go to the Keychain
session.test("transcription")         # {"ok": ..., "message": ...}
```

HTTP API (all `/api/*` need `X-Meetcap-Token`):
- `GET /api/state`: config values, which keys are set, environment, presets and pets.
- `POST /api/save` with `{config, secrets}`. For a secret, `""` keeps it and `false` removes it.
- `POST /api/test` with `{what}`.
- `POST /api/obs` starts OBS setup, and `GET /api/obs` reports its progress.
- `POST /api/finish`.
- `POST /api/choose-folder`.

## Adapters (swap any of them)

```python
class MicSource(Protocol):   def active_bundles(self) -> set[str]
class Recorder(Protocol):    def ensure_ready(self); def start(self); def stop(self) -> Path | None; def is_recording(self) -> bool
class BlobStore(Protocol):   def upload_file(self, local, blob, metadata=None) -> UploadResult
                             def upload_text(self, text, blob) -> UploadResult
                             def size(self, blob) -> int | None; def url(self, blob) -> str
class Transcriber(Protocol): def transcribe(self, audio: Path) -> Transcript
class Summarizer(Protocol):  def summarize(self, transcript_text: str, meta: dict, frames: list[Frame]) -> dict
```

Shipped implementations:

- **`MicWatch(path)`** runs the Swift helper. It also has `processes(include_all=False)` and `available()`.
- **`ObsRecorder(cfg, password_fn)`** controls OBS. It also has:
  - `launch(timeout=None)`, which clears OBS crash markers first.
  - `quit_obs()`: Apple Events, then SIGTERM.
  - `set_video_enabled(bool)` and `setup(set_password_fn)`.
  - `ensure_ready()`, which switches OBS back to the MeetCap profile and collection.
- **`LocalStore(root)`** keeps meetings in a folder. It hard-links recordings into place when source and destination are on the same disk, and copies them into synced folders such as OneDrive, Dropbox, iCloud or Google Drive. It refuses paths outside `root`, and has the same read methods as Azure.
- **`AzureBlobStore(StorageConfig, secrets, credential=None)`** uploads with `validate_content=True` and a size check afterwards, and stores Content-MD5. It also has `check_access()`.
- **`AzureFastTranscriber(SpeechConfig, secrets, credential=None, session=None)`** calls `POST {endpoint}/speechtotext/transcriptions:transcribe?api-version=…` with multipart `audio` and `definition` fields. The definition is `{locales, diarization{enabled,maxSpeakers}, phraseList{phrases}, profanityFilterMode}`. It splits audio longer than 4 h. It raises `TransientError` (429/5xx/network) or `TranscriptionError`.
- **`OpenAICompatTranscriber(client, model, fmt="", chunk_seconds=None, language=None, phrases=None)`** calls `/audio/transcriptions` on any OpenAI-compatible API. That covers Groq, OpenRouter and OpenAI.
- **`AzureOpenAITranscriber(OpenAIConfig, secrets, credential=None, client=None, language=None, phrases=None)`** is the same, for an Azure OpenAI transcription deployment (gpt-4o-transcribe-diarize, whisper or gpt-4o-transcribe). It finds the richest response format the deployment accepts (`diarized_json`, then `verbose_json`, then `json`) and sends audio in chunks under the 25 MB limit.
- **`LocalWhisperTranscriber(model, language=None, phrases=None)`** runs Whisper on Apple Silicon (mlx-whisper) in a short-lived subprocess (`python -m meetcap.local_whisper`), so the model's memory is freed after each meeting. Segments Whisper marks as probably silent are dropped.
- **`merge_speaker_tracks(mine, others)`** merges the per-track transcripts from your mic and the meeting audio into S1 = you and S2 and up = others. Echo of the other side on your mic is dropped.
- **`ChatSummarizer(client, model, prompt=None, max_chars=350000, json_mode=True, vision=True)`** writes notes with chat completions on any OpenAI-compatible API: OpenRouter, Groq, OpenAI, Ollama or Azure OpenAI v1. If a model rejects JSON mode, it falls back to taking the JSON out of the text reply.
- **`AnthropicSummarizer(api_key, model, prompt=None, max_chars=350000, vision=True)`** writes notes with Claude through the Messages API over plain HTTPS, including screenshots. Claude can't transcribe.
- **`AzureOpenAISummarizer(OpenAIConfig, secrets, credential=None, prompt=None, client=None)`** uses `OpenAI(base_url=f"{endpoint}/openai/v1/")` with a key or an Entra token provider. It sends JSON-mode chat completions with images as data URIs, retries text-only if the image call fails, and map-reduces long transcripts.

Example: use batch transcription or on-prem Whisper instead.

```python
class WhisperTranscriber:
    def transcribe(self, audio):
        ...  # call your service
        return Transcript(duration_ms, [Phrase(start_ms, dur_ms, text, speaker)], raw=[resp])

agent = MeetCapAgent(cfg, pipeline_factory=lambda: Pipeline(cfg, agent.store, AzureBlobStore(...),
                                                            WhisperTranscriber(), AzureOpenAISummarizer(...)))
```

## Rendering (`meetcap.render`, pure functions)

- `transcript_for_llm(t)`: `[HH:MM:SS] S2: text` per merged turn.
- `transcript_markdown(t, meta, names=None)`, `transcript_vtt(t, names=None)`.
- `notes_markdown(notes, meta, files)`: sections are omitted when empty; action items render as a table.
- `speaker_names(notes)`: builds `{"S2": "Mary"}` from the model's `speakers` entries.
- `fmt_ts(ms)`, `fmt_vtt(ms)`, `turns(phrases, max_gap_ms=2000)`.

Notes JSON contract (`prompts/notes_system.md`, normalized by `summarize.normalize_notes`):

```json
{"title": "", "summary": "", "speakers": [{"label": "S1", "name": null, "evidence": null}],
 "key_points": [], "decisions": [], "action_items": [{"owner": "", "task": "", "due": null, "timestamp": null}],
 "open_questions": [], "risks": [], "on_screen": [{"timestamp": "HH:MM:SS", "what": ""}],
 "key_moments": [{"timestamp": "HH:MM:SS", "what": ""}]}
```

## Notes library (`meetcap.library`, `meetcap.webui`)

```python
from meetcap.library import MeetingLibrary
from meetcap.webui import serve

lib = MeetingLibrary(blob_factory=lambda: blob_store, blob_prefix="alex", audio_cache_mb=1024,
                     jobs_fn=lambda: store.list(limit=50))
lib.sync()                      # -> number of meetings pulled from Blob; sets lib.last_sync / lib.last_sync_error
lib.list("sow friday")          # every term must match; rows ranked by notes hits (x3) over transcript hits
lib.get("a1b2c3d4e5f6")         # {meta, notes, transcript, frames, audio_cached}; timestamps gain a `seconds` field
lib.audio_file(id)              # Path to cached audio.m4a (downloads + converts on first call) or None
httpd = serve(lib, port=8765, status_fn=agent.status_dict)   # background thread; port=0 picks a free one
httpd.shutdown()
```

- `store_local(meta, notes, transcript, frames=[(path, "HH-MM-SS.jpg")], audio=path)` is called by the pipeline; it is idempotent per meeting.
- List rows: `{id, title, summary, started_at, duration_s, app, actions, moments, has_video, score, matched_in, status}`. `matched_in` is a subset of `["notes", "transcript"]`.
- `processing()` returns pending and failed jobs for the "On its way" section.
- HTTP API: `GET /api/meetings?q=`, `GET /api/meetings/<id>`, `GET /api/meetings/<id>/audio` (ranges), `/video` (ranges, streamed from Blob), `/frames/<name>.jpg`, `GET /api/status`, `POST /api/sync` (needs `X-Meetcap: 1`). Errors are JSON `{"error": "..."}` with 400, 403, 404, 416 or 500.
- The agent's `library_url` property gives the address; `ui.library: false` turns the reader off.

### Standalone reader (`meetcap.reader`, `AzureBlobStore`)

```python
blob = AzureBlobStore(cfg.azure.storage, get_secret)
blob.reader_cors(apply=False)          # "present" | "needed" | "unavailable" (sas auth); apply=True merges the rule -> "added"
link, expires = blob.reader_link(cfg.azure.storage.prefix, days=365)   # rl SAS bound to policy "meetcap-reader"
blob.revoke_reader_link()              # True if a policy was removed (key auth only)

from meetcap.reader import build_reader, check_link, READER_FILE
check_link(link)                       # {host, container, prefix, expires}; ReaderLinkError if it isn't a reader link
build_reader(link, out=READER_FILE, expires=expires)   # writes the HTML (0600) with link, sprites and CSP inside

from meetcap.reader import publish_static
publish_static(cfg.local_root)        # local folder: Meeting Notes.html + meetcap-index.js + reader.js per meeting
```

`StorageError` covers Azure's limits (5 access policies per container, 5 CORS rules per account) and a SAS without read and list.

## Media helpers (`meetcap.media`)

- `probe(path)`, `duration_seconds(path)`, `has_stream(path, "audio"|"video")`
- `wait_until_stable(path, interval=2, checks=3, timeout=180)`
- `extract_audio(src, dst, bitrate="32k")`: mono, 16 kHz, Opus
- `split_audio(src, out_dir, segment_seconds) -> [(path, offset_s)]`
- `extract_scene_frames(video, out_dir, max_frames=24, threshold=0.2, width=1280) -> [Frame]`: scene-change frames, with one every 5 min as a fallback

ffmpeg is looked up on `PATH`, then in `/opt/homebrew/bin` and `/usr/local/bin`.

## OBS client (`meetcap.obs_client`)

`ObsClient(host, port, password, timeout=10).call(request_type, data=None) -> dict` is an obs-websocket v5 client with no event subscriptions. Calls are serialized and it reconnects once transparently.

- Errors: `ObsConnectionError` (unreachable, bad password) and `ObsRequestError(code, comment)`.
- Codes you'll see: `500` output already running, `501` output not running, `600` not found, `601` already exists.

## CLI

| Command | Purpose |
|---|---|
| `meetcap init [--force]` | Write the config template |
| `meetcap secret set\|delete\|list [name]` | Keychain secrets |
| `meetcap setup-obs` | Configure OBS: websocket, profile, scene, sources, output |
| `meetcap doctor [--deep]` | End-to-end checks; `--deep` makes real Azure calls |
| `meetcap mics [--all]` | Processes holding the mic, and the matched meeting app |
| `meetcap menubar` / `meetcap run` | Agent with or without UI (launchd runs `menubar`) |
| `meetcap process FILE [--app X] [--now]` | Queue or process an existing recording |
| `meetcap jobs [--status S] [--limit N]` / `show ID` / `retry [ID]` | Inspect and recover jobs |
| `meetcap status` / `start` / `stop` / `auto on\|off` | Control a running agent from Terminal |
| `meetcap pet on\|off\|test` | Show, hide or test the desktop puppy |
| `meetcap reader [--days N] [--print] [--revoke] [--yes] [--no-open]` | Write the standalone reader (reads Azure directly); print, renew or revoke its link |
| `meetcap setup [--no-open]` | The setup screen in your browser |
| `meetcap pet list` / `meetcap pet <name>` | List the ten pets, or switch to one |
| `meetcap notes [--local] [--serve]` | Open the reader (the standalone file when it exists; `--local` for http://127.0.0.1:8765); `--serve` runs the local library in the foreground |

Exit code is non-zero on failure (for example, `doctor` finding problems).

## Error handling patterns

- **Recording path.** Errors never propagate to the UI. They set `agent.last_error` and fire a notification ("Recording did NOT start"), and the detector retries every 30 s.
- **Processing path.** Every exception is caught per stage and stored on the job. Transient and config errors back off; `PermanentError` dead-letters.
- **Unconfigured cloud.** Recording continues and jobs wait. `agent.last_error` says what is missing.
- **Local deletion.** Only in `cleanup`, and only after `blob.size()` matches every recorded upload.
