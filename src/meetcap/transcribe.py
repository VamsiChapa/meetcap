"""Azure AI Speech fast transcription adapter (synchronous REST, diarization, phrase list).

Docs: https://learn.microsoft.com/azure/ai-services/speech-service/fast-transcription-create
Limits: audio < 5 h and < 500 MB per request. We split at 4 h to leave margin.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Protocol

import requests

from . import media
from .config import SpeechConfig
from .models import Phrase, Transcript
from .vault import SecretGetter, require

log = logging.getLogger(__name__)

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


class TransientError(RuntimeError):
    """Worth retrying with backoff (429, 5xx, network)."""


class TranscriptionError(RuntimeError):
    pass


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> Transcript: ...


def parse_phrases(raw: dict[str, Any], offset_ms: int = 0) -> list[Phrase]:
    phrases = []
    for p in raw.get("phrases", []):
        text = (p.get("text") or "").strip()
        if not text:
            continue
        phrases.append(Phrase(
            start_ms=int(p.get("offsetMilliseconds", 0)) + offset_ms,
            duration_ms=int(p.get("durationMilliseconds", 0)),
            text=text,
            speaker=p.get("speaker"),
            locale=p.get("locale"),
        ))
    return phrases


class AzureFastTranscriber:
    MAX_SECONDS = 4 * 3600
    MAX_BYTES = 450 * 1024 * 1024

    def __init__(self, cfg: SpeechConfig, secrets: SecretGetter, credential=None, session=None):
        self.cfg = cfg
        self._secrets = secrets
        self._credential = credential
        self._http = session or requests.Session()

    @property
    def url(self) -> str:
        return (f"{self.cfg.endpoint.rstrip('/')}/speechtotext/transcriptions:transcribe"
                f"?api-version={self.cfg.api_version}")

    def definition(self) -> dict[str, Any]:
        d: dict[str, Any] = {"profanityFilterMode": self.cfg.profanity_filter}
        if self.cfg.locales:
            d["locales"] = list(self.cfg.locales)
        if self.cfg.max_speakers and self.cfg.max_speakers > 1:
            d["diarization"] = {"enabled": True, "maxSpeakers": int(self.cfg.max_speakers)}
        if self.cfg.phrase_list:
            d["phraseList"] = {"phrases": list(self.cfg.phrase_list)}
        return d

    def _headers(self) -> dict[str, str]:
        if self.cfg.auth == "aad":
            if self._credential is None:
                from azure.identity import DefaultAzureCredential
                self._credential = DefaultAzureCredential()
            return {"Authorization": f"Bearer {self._credential.get_token(COGNITIVE_SCOPE).token}"}
        return {"Ocp-Apim-Subscription-Key": require(self._secrets, "speech-key")}

    def _post(self, audio: Path) -> dict[str, Any]:
        try:
            with open(audio, "rb") as fh:
                resp = self._http.post(
                    self.url,
                    headers=self._headers(),
                    files={"audio": (audio.name, fh, "audio/ogg")},
                    data={"definition": json.dumps(self.definition())},
                    timeout=self.cfg.timeout_seconds,
                )
        except requests.RequestException as exc:
            raise TransientError(f"network error calling Speech: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"Speech returned {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise TranscriptionError(f"Speech returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def transcribe(self, audio: Path) -> Transcript:
        duration = media.duration_seconds(audio)
        size = audio.stat().st_size
        if duration <= self.MAX_SECONDS and size <= self.MAX_BYTES:
            raw = self._post(audio)
            return Transcript(int(raw.get("durationMilliseconds") or duration * 1000), parse_phrases(raw), [raw])

        log.info("audio is %.1f h; splitting for fast transcription", duration / 3600)
        phrases: list[Phrase] = []
        raws: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="meetcap-split-") as tmp:
            for part, offset in media.split_audio(audio, Path(tmp), self.MAX_SECONDS):
                raw = self._post(part)
                raws.append(raw)
                phrases.extend(parse_phrases(raw, int(offset * 1000)))
        return Transcript(int(duration * 1000), phrases, raws)


# =============================================================================================
# Azure OpenAI transcription (/audio/transcriptions): for people who only have an Azure OpenAI
# resource. Works with gpt-4o-transcribe-diarize (speaker labels), whisper (timestamps) and
# gpt-4o(-mini)-transcribe (text only; timestamps are spread across each chunk).
# Limit: 25 MB per request, so audio is sent in chunks (Opus at 32 kbps is ~0.25 MB/min).
# =============================================================================================
TRANSCRIBE_FORMATS = ("diarized_json", "verbose_json", "json")  # best first; probed once, then remembered
TRANSCRIBE_DATED_API = "2025-03-01-preview"  # dated API that serves the gpt-4o transcribe models


def _speaker_id(value: Any) -> int | None:
    """'A' -> 1, 'B' -> 2, 'speaker_0' -> 1, 3 -> 4."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value + 1
    s = str(value).strip()
    if len(s) == 1 and s.isalpha():
        return ord(s.upper()) - ord("A") + 1
    digits = re.findall(r"\d+", s)
    return int(digits[-1]) + 1 if digits else None


def parse_openai_transcription(raw: dict[str, Any], offset_ms: int = 0, span_ms: int = 0) -> list[Phrase]:
    phrases: list[Phrase] = []
    segments = raw.get("segments") or []
    if segments:
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or start)
            phrases.append(Phrase(int(start * 1000) + offset_ms, max(0, int((end - start) * 1000)), text,
                                  _speaker_id(seg.get("speaker"))))
        return phrases
    # Text-only response: split into sentences and spread them across the chunk by length.
    text = (raw.get("text") or "").strip()
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    total = sum(len(s) for s in sentences) or 1
    t = 0.0
    for s in sentences:
        dur = span_ms * len(s) / total
        phrases.append(Phrase(offset_ms + int(t), int(dur), s.strip()))
        t += dur
    return phrases


class OpenAICompatTranscriber:
    """/audio/transcriptions on any OpenAI-compatible service: OpenAI, Groq, OpenRouter, Azure OpenAI.

    Audio goes in chunks (25 MB request limits; OpenRouter's upstream times out after 60 s) and the richest
    response format the model accepts is found once: diarized_json (speakers) > verbose_json (timestamps) > json."""
    CHUNK_SECONDS = {"diarized_json": 20 * 60, "verbose_json": 10 * 60, "json": 5 * 60}

    def __init__(self, client, model: str, fmt: str = "", chunk_seconds: int | None = None,
                 language: str | None = None, phrases: list[str] | None = None, prompt_limit: int = 800,
                 label: str = "transcription"):
        self._client, self.model, self.fmt = client, model, fmt
        self.chunk_seconds, self.language, self.label = chunk_seconds, language, label
        self.prompt = ", ".join(phrases or [])[:prompt_limit]

    def _call(self, path: Path, fmt: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.model, "response_format": fmt}
        if self.language:
            kwargs["language"] = self.language
        extra: dict[str, Any] = {}
        if fmt == "diarized_json":
            extra["chunking_strategy"] = "auto"  # required by the diarize model for audio longer than 30 s
        elif fmt == "verbose_json":
            extra["timestamp_granularities"] = ["segment"]
        if self.prompt and fmt != "diarized_json":
            kwargs["prompt"] = f"Vocabulary: {self.prompt}."
        with open(path, "rb") as fh:
            try:
                resp = self._client.audio.transcriptions.create(file=fh, **kwargs, **extra)
            except TypeError:  # older SDK without these keyword arguments
                fh.seek(0)
                resp = self._client.audio.transcriptions.create(file=fh, extra_body=extra, **kwargs)
        if isinstance(resp, dict):
            return resp
        if hasattr(resp, "model_dump"):
            return resp.model_dump()
        return {"text": str(resp)}

    def detect_format(self, probe: Path) -> str:
        """Find the richest response format this model accepts."""
        last: Exception | None = None
        for fmt in TRANSCRIBE_FORMATS:
            try:
                self._call(probe, fmt)
                return fmt
            except Exception as exc:  # noqa: BLE001 - unsupported format comes back as a 400
                if not _is_bad_request(exc):
                    raise
                last = exc
        raise TranscriptionError(f"model '{self.model}' rejected every format: {last}")

    def transcribe(self, audio: Path) -> Transcript:
        duration = media.duration_seconds(audio)
        if not self.fmt:
            self.fmt = self.detect_format(audio)
        chunk = self.chunk_seconds or self.CHUNK_SECONDS[self.fmt]
        phrases: list[Phrase] = []
        raws: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="meetcap-stt-") as tmp:
            parts = [(audio, 0.0)] if duration <= chunk else media.split_audio(audio, Path(tmp), chunk)
            for path, offset in parts:
                span = int(min(chunk, max(duration - offset, 0.0)) * 1000)
                try:
                    raw = self._call(path, self.fmt)
                except Exception as exc:  # noqa: BLE001
                    if _is_transient(exc):
                        raise TransientError(f"{self.label}: {exc}") from exc
                    raise
                raws.append(raw)
                phrases.extend(parse_openai_transcription(raw, int(offset * 1000), span))
        return Transcript(int(duration * 1000), phrases, raws)


class AzureOpenAITranscriber(OpenAICompatTranscriber):
    """A transcription deployment in an Azure OpenAI resource (config: azure.openai.transcribe_*)."""

    def __init__(self, cfg, secrets: SecretGetter, credential=None, client=None, language: str | None = None,
                 phrases: list[str] | None = None):
        from .summarize import make_client

        self.cfg = cfg
        if client is None:
            if cfg.auth == "aad":
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
                api_key: Any = get_bearer_token_provider(credential or DefaultAzureCredential(), COGNITIVE_SCOPE)
            else:
                api_key = require(secrets, "openai-key")
            client = make_client(cfg.endpoint, api_key, cfg.transcribe_api_version, cfg.timeout_seconds)
        super().__init__(client, cfg.transcribe_deployment, cfg.transcribe_format or "", language=language,
                         phrases=phrases, label="Azure OpenAI transcription")


def _is_bad_request(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400 or "400" in type(exc).__name__ or "BadRequest" in type(exc).__name__


def _is_transient(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None)
    return code == 429 or (isinstance(code, int) and code >= 500) or "Connection" in type(exc).__name__ \
        or "Timeout" in type(exc).__name__


# =============================================================================================
# On-Mac Whisper (Apple Silicon, mlx-whisper). Free and private; no speaker labels by itself,
# which the "split speakers" tracks make up for (your mic vs. the meeting audio).
# =============================================================================================
class LocalWhisperTranscriber:
    NO_SPEECH_DROP = 0.6  # Whisper invents "Thank you." on silence; drop segments it thinks are silent

    def __init__(self, model: str, language: str | None = None, phrases: list[str] | None = None):
        self.model, self.language = model, language
        self.prompt = ("Vocabulary: " + ", ".join(phrases) + ".") if phrases else None

    def transcribe(self, audio: Path) -> Transcript:
        from . import local_whisper

        ok, why = local_whisper.supported()
        if not ok:
            raise TranscriptionError(why)
        duration = media.duration_seconds(audio)
        quiet = media.silences(audio)
        if duration <= 0 or sum(e - s for s, e in quiet) >= 0.97 * duration:
            log.info("%s is silent; nothing to transcribe", audio.name)  # Whisper would invent text here
            return Transcript(int(duration * 1000), [], [{"skipped": "silent"}])
        with tempfile.TemporaryDirectory(prefix="meetcap-whisper-") as tmp:
            raw = local_whisper.run(audio, Path(tmp) / "out.json", self.model, self.language, self.prompt)
        phrases: list[Phrase] = []
        for seg in raw.get("segments", []):
            text = (seg.get("text") or "").strip()
            start, end = float(seg.get("start", 0)), min(float(seg.get("end", 0)), duration)
            if not text or start >= duration or end <= start:
                continue  # past the end of the audio: always invented
            if float(seg.get("no_speech_prob", 0)) >= self.NO_SPEECH_DROP:
                continue
            if _overlap(start, end, quiet) >= 0.8 * (end - start):
                continue  # Whisper "heard" words in a silent stretch
            phrases.append(Phrase(int(start * 1000), int((end - start) * 1000), text))
        return Transcript(int(duration * 1000), phrases, [raw])


def _overlap(start: float, end: float, spans: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(end, b) - max(start, a)) for a, b in spans)


# =============================================================================================
# Split-speaker merge: your mic track becomes S1, the meeting (system audio) track S2.. .
# =============================================================================================
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def merge_speaker_tracks(mine: Transcript, others: Transcript, duration_ms: int | None = None) -> Transcript:
    """Combine per-track transcripts. Drops lines on your mic that are just the other side leaking in
    from your speakers (same words, same moment, already on the meeting track)."""
    from difflib import SequenceMatcher

    theirs = [Phrase(p.start_ms, p.duration_ms, p.text, 2 + ((p.speaker or 1) - 1), p.locale) for p in others.phrases]
    kept: list[Phrase] = []
    for m in mine.phrases:
        a = _norm(m.text)
        echo = False
        for o in theirs:
            if o.start_ms > m.end_ms + 1500 or o.end_ms < m.start_ms - 1500:
                continue
            b = _norm(o.text)
            if a and (a in b or SequenceMatcher(None, a, b).ratio() > 0.6):
                echo = True
                break
        if not echo:
            kept.append(Phrase(m.start_ms, m.duration_ms, m.text, 1, m.locale))
    merged = sorted(kept + theirs, key=lambda p: p.start_ms)
    total = duration_ms if duration_ms is not None else max(mine.duration_ms, others.duration_ms)
    return Transcript(total, merged, [{"track": "you", "raw": mine.raw}, {"track": "others", "raw": others.raw}])
