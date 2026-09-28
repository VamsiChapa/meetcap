"""Configuration model and loader. Non-secret settings only; secrets live in the Keychain (see vault.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

APP_NAME = "meetcap"
STATE_DIR = Path(os.environ.get("MEETCAP_HOME", Path.home() / "Library" / "Application Support" / APP_NAME))
LOG_DIR = Path(os.environ.get("MEETCAP_LOG_DIR", Path.home() / "Library" / "Logs" / APP_NAME))
CONFIG_PATH = Path(os.environ.get("MEETCAP_CONFIG", STATE_DIR / "config.yaml"))
DB_PATH = STATE_DIR / "jobs.sqlite3"
WORK_DIR = STATE_DIR / "work"
BIN_DIR = STATE_DIR / "bin"


class ConfigError(ValueError):
    pass


@dataclass
class DetectionConfig:
    enabled: bool = True
    apps: list[str] = field(default_factory=lambda: [
        "com.microsoft.teams2", "com.microsoft.teams", "us.zoom.xos",
        "Cisco-Systems.Spark", "com.cisco.webexmeetingsapp",
    ])
    browsers: list[str] = field(default_factory=list)
    poll_seconds: float = 2.0
    start_after_seconds: float = 3.0
    stop_after_seconds: float = 20.0
    min_meeting_seconds: float = 90.0
    max_meeting_hours: float = 5.0
    micwatch_path: str = ""  # default: BIN_DIR/micwatch


@dataclass
class ObsConfig:
    host: str = "127.0.0.1"
    port: int = 4455
    profile: str = "MeetCap"
    collection: str = "MeetCap"
    scene: str = "MeetCap"
    app_path: str = "/Applications/OBS.app"
    launch_timeout_seconds: float = 45.0


@dataclass
class RecordingConfig:
    video: bool = True
    fps: int = 10
    max_width: int = 1920
    display_index: int = 0
    output_dir: str = "~/Movies/MeetCap"
    keep_obs_running: bool = False  # True = instant start, but macOS mic/screen indicators stay on all day
    obs: ObsConfig = field(default_factory=ObsConfig)


@dataclass
class StoreConfig:
    """Where meetings are kept. Local is the default and needs no account; point `local_path` at a
    OneDrive / Google Drive / Dropbox folder to have that app back it up to the cloud."""
    backend: str = "local"            # local | azure
    local_path: str = "~/MeetCap"


@dataclass
class StorageConfig:
    account_url: str = ""
    container: str = "meetings"
    prefix: str = ""
    auth: str = "aad"  # aad (az login) | key (connection string) | sas (container SAS URL)


@dataclass
class SpeechConfig:
    endpoint: str = ""
    api_version: str = "2025-10-15"
    locales: list[str] = field(default_factory=lambda: ["en-US"])
    max_speakers: int = 8
    profanity_filter: str = "None"
    phrase_list: list[str] = field(default_factory=list)
    auth: str = "key"  # key | aad
    timeout_seconds: float = 900.0


@dataclass
class OpenAIConfig:
    endpoint: str = ""
    deployment: str = "gpt-4.1"
    auth: str = "key"  # key | aad
    api_version: str = ""  # "" = v1 API (/openai/v1/); set e.g. "2024-10-21" for resources that only serve the dated API
    # Used when transcription.engine = openai (a transcription deployment in this same resource)
    transcribe_deployment: str = ""   # e.g. gpt-4o-transcribe-diarize, gpt-4o-transcribe, whisper
    transcribe_format: str = ""       # diarized_json | verbose_json | json; "" = detect on first use
    transcribe_api_version: str = ""  # "" = v1 API
    max_transcript_chars: int = 350_000
    timeout_seconds: float = 300.0


@dataclass
class AzureConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)


@dataclass
class UIConfig:
    icon: str = "puppy"   # puppy (animated character) | dot (tiny text symbol)
    animate: bool = True
    pet: bool = False                  # a pet walks along the bottom of the screen
    pet_species: str = "puppy"         # see `meetcap pet list`
    pet_size: int = 64                 # points
    pet_hide_in_meetings: bool = False  # hide it while recording (if an app ever shows it in a screen share)
    pet_draggable: bool = True         # false = clicks always pass straight through it
    library: bool = True               # meeting notes web app at http://127.0.0.1:<library_port>
    library_port: int = 8765
    library_audio_cache_mb: int = 1024  # local audio kept for instant playback (oldest dropped first)
    open_notes_after_meeting: bool = True  # open the notes reader in your browser when a meeting's notes are ready


TRANSCRIPTION_ENGINES = ("local", "groq", "openrouter", "openai", "azure-openai", "azure-speech", "none")
SUMMARY_PROVIDERS = ("none", "openrouter", "groq", "anthropic", "openai", "azure-openai", "ollama")
STORAGE_BACKENDS = ("local", "azure")


@dataclass
class TranscriptionConfig:
    # local        = Whisper on this Mac (free, private; Apple Silicon; ~1.6 GB model downloaded once)
    # groq         = Whisper on Groq (free tier: ~8 h of audio a day)      | openrouter = Whisper via OpenRouter
    # openai       = OpenAI (or any OpenAI-compatible server via base_url)  | none = keep the audio, no transcript
    # azure-openai = a transcription deployment in Azure OpenAI             | azure-speech = Azure AI Speech
    engine: str = "local"
    local_model: str = "mlx-community/whisper-large-v3-turbo"
    model: str = ""       # groq / openrouter / openai: "" = the provider's default Whisper model
    base_url: str = ""    # openai only: a different OpenAI-compatible server
    # Record your mic and the meeting audio on separate tracks and transcribe them separately, so the
    # transcript says "You" vs "Others" even when the engine can't tell voices apart. auto = on for local.
    split_speakers: str = "auto"  # auto | on | off


@dataclass
class SummaryConfig:
    """Who writes the notes (summary, action items, highlights). none = transcript only."""
    provider: str = "none"   # none | openrouter | groq | anthropic | openai | azure-openai | ollama
    model: str = ""          # "" = the provider's default (see providers.py)
    base_url: str = ""       # openai / ollama: a different OpenAI-compatible server
    max_transcript_chars: int = 0  # 0 = provider default; longer transcripts are summarised in parts
    screenshots: bool = True       # send screen frames when the model can see images


@dataclass
class ProcessingConfig:
    pause_while_recording: bool = True
    delete_local_after_upload: bool = True
    screen_insights: bool = True
    max_frames: int = 24
    notes_dir: str = ""
    open_notes_when_ready: bool = False
    user_name: str = ""
    prompt_file: str = ""
    max_attempts: int = 8
    audio_bitrate: str = "32k"


@dataclass
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    storage: StoreConfig = field(default_factory=StoreConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    azure: AzureConfig = field(default_factory=AzureConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    # Resolved paths -------------------------------------------------------
    @property
    def output_dir(self) -> Path:
        return expand(self.recording.output_dir)

    @property
    def micwatch_path(self) -> Path:
        return expand(self.detection.micwatch_path) if self.detection.micwatch_path else BIN_DIR / "micwatch"

    @property
    def split_speakers(self) -> bool:
        mode = self.transcription.split_speakers
        return mode == "on" or (mode == "auto" and self.transcription.engine == "local")

    @property
    def notes_dir(self) -> Path | None:
        return expand(self.processing.notes_dir) if self.processing.notes_dir else None

    @property
    def local_root(self) -> Path:
        return expand(self.storage.local_path)

    @property
    def blob_prefix(self) -> str:
        """Top folder for meetings inside the store (Azure: optional per-person folder)."""
        return self.azure.storage.prefix.strip("/") if self.storage.backend == "azure" else ""

    @property
    def reader_path(self) -> Path:
        """The notes reader page that opens in your browser."""
        if self.storage.backend == "local":
            return self.local_root / "Meeting Notes.html"
        return STATE_DIR / "Meeting Notes.html"


def expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _build(cls: type, data: Any, path: str = "") -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"'{path or 'root'}' must be a mapping")
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"Unknown key(s) under '{path or 'root'}': {', '.join(unknown)}")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        t = hints[f.name]
        value = data[f.name]
        kwargs[f.name] = _build(t, value, f"{path}.{f.name}".lstrip(".")) if is_dataclass(t) else value
    return cls(**kwargs)


def _placeholder(v: Any) -> bool:
    return not v or "<" in str(v)


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Bring older config files forward. Everything that worked before keeps working."""
    az = raw.get("azure") if isinstance(raw.get("azure"), dict) else {}
    if "transcriber" in az:  # 0.2.x kept this under azure:
        raw.setdefault("transcription", {}).setdefault("engine", az.pop("transcriber"))
    tr = raw.get("transcription")
    if isinstance(tr, dict):
        tr["engine"] = {"speech": "azure-speech", "openai": "azure-openai"}.get(tr.get("engine"), tr.get("engine"))
    st = az.get("storage") if isinstance(az.get("storage"), dict) else {}
    if "storage" not in raw and st and (st.get("auth") in ("key", "sas") or not _placeholder(st.get("account_url"))):
        raw["storage"] = {"backend": "azure"}           # 0.8 and earlier always stored in Azure Blob
    oa = az.get("openai") if isinstance(az.get("openai"), dict) else {}
    if "summary" not in raw and oa and not _placeholder(oa.get("endpoint")):
        raw["summary"] = {"provider": "azure-openai"}   # 0.8 and earlier always summarised with Azure OpenAI
    return raw


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        return Config()
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return _build(Config, migrate(raw))


def problems(cfg: Config) -> list[str]:
    """Settings that stop processing (recording still works). Missing keys are reported when used."""
    out: list[str] = []
    s, sp, oa = cfg.azure.storage, cfg.azure.speech, cfg.azure.openai
    backend, engine, summary = cfg.storage.backend, cfg.transcription.engine, cfg.summary.provider
    if backend not in STORAGE_BACKENDS:
        out.append(f"storage.backend must be one of: {', '.join(STORAGE_BACKENDS)}")
    if backend == "azure":
        if s.auth not in ("aad", "key", "sas"):
            out.append("azure.storage.auth must be 'aad', 'key' or 'sas'")
        if s.auth == "aad" and _placeholder(s.account_url):
            out.append("azure.storage.account_url is not set")
    if engine not in TRANSCRIPTION_ENGINES:
        out.append(f"transcription.engine must be one of: {', '.join(TRANSCRIPTION_ENGINES)}")
    if engine == "azure-speech" and _placeholder(sp.endpoint):
        out.append("azure.speech.endpoint is not set")
    if engine == "azure-openai" and (_placeholder(oa.endpoint) or not oa.transcribe_deployment):
        out.append("azure.openai.endpoint and azure.openai.transcribe_deployment must be set")
    if cfg.transcription.split_speakers not in ("auto", "on", "off"):
        out.append("transcription.split_speakers must be 'auto', 'on' or 'off'")
    if summary not in SUMMARY_PROVIDERS:
        out.append(f"summary.provider must be one of: {', '.join(SUMMARY_PROVIDERS)}")
    if summary == "azure-openai" and _placeholder(oa.endpoint):
        out.append("azure.openai.endpoint is not set")
    for name, val in (("speech", sp.auth), ("openai", oa.auth)):
        if val not in ("aad", "key"):
            out.append(f"azure.{name}.auth must be 'aad' or 'key'")
    return out


def ensure_dirs(cfg: Config) -> None:
    dirs = [STATE_DIR, LOG_DIR, WORK_DIR, BIN_DIR, cfg.output_dir]
    if cfg.storage.backend == "local":
        dirs.append(cfg.local_root)
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
