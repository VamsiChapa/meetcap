"""Checks shared by `meetcap doctor` and the setup screen. Each returns (ok, message), where ok is
True (works), False (broken, with what to do) or None (switched off, or can't tell)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import providers
from .config import Config
from .vault import SecretGetter

Check = tuple["bool | None", str]


def _guard(fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - every failure becomes a readable line
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return False, first[:300]


def secrets_needed(cfg: Config) -> list[str]:
    """Keychain entries the current choices need."""
    need: list[str] = []
    if cfg.storage.backend == "azure" and cfg.azure.storage.auth in ("key", "sas"):
        need.append("storage-sas-url" if cfg.azure.storage.auth == "sas" else "storage-connection-string")
    for choice in (cfg.transcription.engine, cfg.summary.provider):
        p = providers.PRESETS.get(choice)
        if choice == "azure-speech" and cfg.azure.speech.auth != "key":
            continue
        if choice == "azure-openai" and cfg.azure.openai.auth != "key":
            continue
        if p and p.secret and p.secret not in need:
            need.append(p.secret)
    return need


def storage(cfg: Config, secrets: SecretGetter) -> Check:
    def run() -> Check:
        where = providers.make_store(cfg, secrets).check_access()
        return True, f"{providers.storage_label(cfg.storage.backend)}: can write to {where}"
    return _guard(run)


def transcription(cfg: Config, secrets: SecretGetter) -> Check:
    engine = cfg.transcription.engine
    if engine == "none":
        return None, "transcription is off: meetings keep their audio, without a transcript"
    if engine == "local":
        from . import local_whisper
        ok, why = local_whisper.supported()
        if not ok:
            return False, f"Whisper on this Mac: {why}. Pick Groq (free) or another engine instead."
        if not local_whisper.installed():
            return False, "Whisper on this Mac isn't installed; run ./install.sh again"
        return True, "Whisper on this Mac is installed (the model downloads on first use, about 1.6 GB)"

    def run() -> Check:
        from . import media
        t = providers.make_transcriber(cfg, secrets)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "tone.wav"
            subprocess.run([media._tool("ffmpeg"), "-loglevel", "error", "-f", "lavfi", "-i",
                            "anullsrc=r=16000:cl=mono", "-t", "2", str(wav)], check=True)
            t.transcribe(media.extract_audio(wav, Path(tmp) / "probe.ogg"))
        return True, f"{providers.describe(cfg)['transcription']} answered"
    return _guard(run)


def summary(cfg: Config, secrets: SecretGetter) -> Check:
    if cfg.summary.provider == "none":
        return None, "notes are off: meetings get a transcript and audio, no summary"

    def run() -> Check:
        s = providers.make_summarizer(cfg, secrets)
        reply = s._chat('Reply with exactly this JSON object: {"ok": true}')
        if not isinstance(reply, dict):
            return False, f"unexpected reply: {reply!r}"[:200]
        return True, f"{providers.describe(cfg)['notes']} answered"
    return _guard(run)
