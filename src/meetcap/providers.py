"""Which service does what. One table of presets, three factories: store, transcriber, summarizer.

Everything provider-specific (base URL, default model, Keychain secret, request limits) lives in PRESETS,
so the setup screen, `meetcap doctor` and the pipeline all agree. Defaults checked September 2026:

- Groq: Whisper at /openai/v1/audio/transcriptions, 25 MB per request and ~8 h of audio a day on the free
  tier. Chat models on the free tier allow 8,000 tokens a minute, so long transcripts go in small parts.
- OpenRouter: /api/v1/audio/transcriptions (OpenAI multipart accepted; upstream times out after 60 s,
  so audio goes in short chunks). `openrouter/free` routes to whichever free chat model is available.
- Anthropic: the Messages API takes text and images, not audio, so Claude writes notes but can't transcribe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Config, expand
from .vault import SecretGetter, require

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str = ""
    secret: str | None = None          # Keychain name of the API key (None = no key needed)
    stt_model: str = ""                # "" = can't transcribe
    stt_format: str = ""               # "" = probe
    stt_chunk_seconds: int = 0         # 0 = by format
    chat_model: str = ""               # "" = can't write notes
    json_mode: bool = True
    vision: bool = False
    max_chars: int = 350_000           # transcript characters per request before splitting
    max_retries: int = 4
    signup: str = ""
    free: str = ""                     # one line on the free option, shown in setup


PRESETS: dict[str, Preset] = {
    "groq": Preset("Groq", "https://api.groq.com/openai/v1", "groq-key",
                   stt_model="whisper-large-v3-turbo", stt_format="verbose_json", stt_chunk_seconds=20 * 60,
                   chat_model="openai/gpt-oss-120b", json_mode=True, vision=False, max_chars=16_000, max_retries=8,
                   signup="https://console.groq.com/keys",
                   free="Free tier, no card: about 8 hours of audio a day; notes for long meetings take a few minutes"),
    "openrouter": Preset("OpenRouter", "https://openrouter.ai/api/v1", "openrouter-key",
                         stt_model="openai/whisper-large-v3", stt_format="verbose_json", stt_chunk_seconds=8 * 60,
                         chat_model="openrouter/free", json_mode=True, vision=False, max_chars=60_000, max_retries=6,
                         signup="https://openrouter.ai/keys",
                         free="Notes: free models via openrouter/free (they change, and some log prompts). "
                              "Transcription is pay-as-you-go"),
    "openai": Preset("OpenAI", "https://api.openai.com/v1", "openai-api-key",
                     stt_model="whisper-1", stt_format="verbose_json", stt_chunk_seconds=10 * 60,
                     chat_model="gpt-5-mini", json_mode=True, vision=True, signup="https://platform.openai.com/api-keys"),
    "anthropic": Preset("Claude (Anthropic)", secret="anthropic-key", chat_model="claude-haiku-4-5-20251001",
                        vision=True, signup="https://console.anthropic.com/settings/keys"),
    "ollama": Preset("Ollama (on this Mac)", "http://127.0.0.1:11434/v1", None, chat_model="llama3.2",
                     json_mode=True, vision=False, max_chars=24_000, signup="https://ollama.com/download",
                     free="Free and private: runs a model on your Mac (install Ollama, then `ollama pull llama3.2`)"),
    "azure-openai": Preset("Azure OpenAI", secret="openai-key", vision=True),
    "azure-speech": Preset("Azure AI Speech", secret="speech-key"),
    "local": Preset("Whisper on this Mac", free="Free and private on Apple Silicon; downloads a 1.6 GB model once"),
}


def storage_label(backend: str) -> str:
    return {"local": "Folder on this Mac", "azure": "Azure Blob Storage"}.get(backend, backend)


# -- factories --------------------------------------------------------------------------------------------
def _openai_client(base_url: str, api_key: str, timeout: float = 300.0, max_retries: int = 4):
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout, max_retries=max_retries)


def _azure_credential(cfg: Config):
    az = cfg.azure
    uses = [az.storage.auth if cfg.storage.backend == "azure" else "",
            az.speech.auth if cfg.transcription.engine == "azure-speech" else "",
            az.openai.auth if "azure-openai" in (cfg.transcription.engine, cfg.summary.provider) else ""]
    if "aad" not in uses:
        return None
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


def make_store(cfg: Config, secrets: SecretGetter, credential=None):
    if cfg.storage.backend == "azure":
        from .storage import AzureBlobStore
        return AzureBlobStore(cfg.azure.storage, secrets, credential)
    from .storage import LocalStore
    return LocalStore(cfg.local_root)


def _language(cfg: Config) -> str | None:
    locales = cfg.azure.speech.locales or []
    return locales[0].split("-")[0] if len(locales) == 1 else None   # mixed languages: auto-detect


def make_transcriber(cfg: Config, secrets: SecretGetter, credential=None):
    """None when transcription is off: the audio is still kept and playable."""
    engine = cfg.transcription.engine
    vocab = cfg.azure.speech.phrase_list
    if engine == "none":
        return None
    if engine == "local":
        from .transcribe import LocalWhisperTranscriber
        return LocalWhisperTranscriber(cfg.transcription.local_model, _language(cfg), vocab)
    if engine == "azure-speech":
        from .transcribe import AzureFastTranscriber
        return AzureFastTranscriber(cfg.azure.speech, secrets, credential)
    if engine == "azure-openai":
        from .transcribe import AzureOpenAITranscriber
        return AzureOpenAITranscriber(cfg.azure.openai, secrets, credential, language=_language(cfg), phrases=vocab)
    p = PRESETS[engine]
    from .transcribe import OpenAICompatTranscriber
    base = (cfg.transcription.base_url if engine == "openai" and cfg.transcription.base_url else p.base_url)
    client = _openai_client(base, require(secrets, p.secret), max_retries=p.max_retries)
    return OpenAICompatTranscriber(client, cfg.transcription.model or p.stt_model, p.stt_format,
                                   p.stt_chunk_seconds or None, _language(cfg), vocab,
                                   prompt_limit=800 if engine == "groq" else 2000, label=f"{p.label} transcription")


def make_summarizer(cfg: Config, secrets: SecretGetter, credential=None):
    """None when notes are off: meetings still get a transcript and audio in the reader."""
    provider = cfg.summary.provider
    if provider == "none":
        return None
    prompt = expand(cfg.processing.prompt_file).read_text() if cfg.processing.prompt_file else None
    if provider == "azure-openai":
        from .summarize import AzureOpenAISummarizer
        s = AzureOpenAISummarizer(cfg.azure.openai, secrets, credential, prompt)
        if cfg.summary.max_transcript_chars:
            s.max_chars = cfg.summary.max_transcript_chars
        s.vision = cfg.summary.screenshots
        return s
    p = PRESETS[provider]
    model = cfg.summary.model or p.chat_model
    max_chars = cfg.summary.max_transcript_chars or p.max_chars
    vision = p.vision and cfg.summary.screenshots
    if provider == "anthropic":
        from .summarize import AnthropicSummarizer
        return AnthropicSummarizer(require(secrets, p.secret), model, prompt, max_chars, vision)
    from .summarize import ChatSummarizer
    base = cfg.summary.base_url if provider in ("openai", "ollama") and cfg.summary.base_url else p.base_url
    key = require(secrets, p.secret) if p.secret else ""
    client = _openai_client(base, key, max_retries=p.max_retries)
    return ChatSummarizer(client, model, prompt, max_chars, p.json_mode, vision)


def describe(cfg: Config) -> dict[str, Any]:
    """One line per role, for status output and the setup screen."""
    tr, sm = cfg.transcription, cfg.summary
    tp, sp = PRESETS.get(tr.engine), PRESETS.get(sm.provider)
    return {
        "storage": storage_label(cfg.storage.backend) + (f" ({cfg.local_root})" if cfg.storage.backend == "local" else ""),
        "transcription": "off" if tr.engine == "none" else
        (tp.label if tp else tr.engine) + (f" · {tr.model or tp.stt_model}" if tp and tp.stt_model else ""),
        "notes": "off" if sm.provider == "none" else
        (sp.label if sp else sm.provider) + (f" · {sm.model or sp.chat_model}" if sp and sp.chat_model else ""),
    }
