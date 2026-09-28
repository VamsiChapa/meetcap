"""Notes writers. One request shape (OpenAI chat parts); adapters for OpenAI-compatible APIs and Anthropic."""
from __future__ import annotations

import base64
import json
import logging
import re
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from .config import OpenAIConfig
from .models import Frame
from .render import fmt_ts
from .vault import SecretGetter, require

log = logging.getLogger(__name__)

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
NOTE_KEYS = ("title", "summary", "speakers", "key_points", "decisions", "action_items",
             "open_questions", "risks", "on_screen", "key_moments")


class Summarizer(Protocol):
    def summarize(self, transcript_text: str, meta: dict[str, Any], frames: list[Frame]) -> dict[str, Any]: ...


def default_prompt() -> str:
    return resources.files("meetcap").joinpath("prompts/notes_system.md").read_text(encoding="utf-8")


def normalize_notes(notes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in NOTE_KEYS:
        v = notes.get(k)
        if k in ("title", "summary"):
            out[k] = (v or "").strip() if isinstance(v, str) else ""
        else:
            out[k] = v if isinstance(v, list) else []
    out["title"] = out["title"] or "Untitled meeting"
    return out


def make_client(endpoint: str, api_key: Any, api_version: str = "", timeout: float = 300.0, max_retries: int = 4):
    """v1 API client by default; the dated API (AzureOpenAI) when `api_version` is set."""
    endpoint = endpoint.rstrip("/")
    if api_version:
        from openai import AzureOpenAI

        if callable(api_key):
            return AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, azure_ad_token_provider=api_key,
                               max_retries=max_retries, timeout=timeout)
        return AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, api_key=api_key,
                           max_retries=max_retries, timeout=timeout)
    from openai import OpenAI

    return OpenAI(base_url=f"{endpoint}/openai/v1/", api_key=api_key, max_retries=max_retries, timeout=timeout)


def split_text(text: str, max_chars: int) -> list[str]:
    """Split on line boundaries into chunks no longer than max_chars."""
    chunks, current, size = [], [], 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > max_chars and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model reply, tolerating code fences or a sentence around it."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class NotesWriter:
    """Builds the request (metadata, transcript, screenshots; map-reduce for long meetings) and parses the
    reply. Subclasses only implement `_chat(user_content)`, where user_content is a string or a list of
    OpenAI-style parts ({"type": "text"} / {"type": "image_url"})."""
    max_chars = 350_000
    vision = True
    prompt = ""

    def _chat(self, user_content: Any) -> dict[str, Any]:
        raise NotImplementedError

    def summarize(self, transcript_text: str, meta: dict[str, Any], frames: list[Frame]) -> dict[str, Any]:
        header = "Meeting metadata (JSON):\n" + json.dumps(meta, indent=2) + "\n\n"
        if len(transcript_text) > self.max_chars:
            # Map-reduce for long meetings (or small free-tier limits): notes per part, then one merge pass.
            parts = split_text(transcript_text, self.max_chars)
            log.info("transcript is %d chars; summarizing in %d parts", len(transcript_text), len(parts))
            partial = [self._chat(header + f"Segment {i + 1} of {len(parts)}. Transcript:\n{p}")
                       for i, p in enumerate(parts)]
            body = ("The transcript was too long for one pass. These are JSON notes for consecutive segments; "
                    "merge them into one set of notes and de-duplicate:\n" + json.dumps(partial))
        else:
            body = "Transcript:\n" + (transcript_text or "(no speech detected)")
        frames = frames if self.vision else []
        content: list[dict[str, Any]] = [{"type": "text", "text": header + body}]
        for fr in frames:
            data = base64.b64encode(Path(fr.path).read_bytes()).decode()
            content.append({"type": "text", "text": f"Screenshot at {fmt_ts(fr.t_ms)}:"})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": "high"}})
        try:
            return normalize_notes(self._chat(content))
        except Exception as exc:  # noqa: BLE001
            if not frames:
                raise
            # Model without vision, or image payload too large: fall back to text only.
            log.warning("summary with screenshots failed (%s); retrying text-only", exc)
            return normalize_notes(self._chat(header + body))


class ChatSummarizer(NotesWriter):
    """Chat completions on any OpenAI-compatible API: OpenAI, OpenRouter, Groq, Ollama, Azure OpenAI v1."""

    def __init__(self, client, model: str, prompt: str | None = None, max_chars: int = 350_000,
                 json_mode: bool = True, vision: bool = True):
        self._client, self.model = client, model
        self.prompt = prompt or default_prompt()
        self.max_chars, self.json_mode, self.vision = max_chars, json_mode, vision

    def _chat(self, user_content: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.model, "messages": [
            {"role": "system", "content": self.prompt}, {"role": "user", "content": user_content}]}
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - some models reject JSON mode; the prompt asks for JSON anyway
            if not (self.json_mode and _bad_request(exc)):
                raise
            log.info("%s doesn't take JSON mode; asking for JSON in the prompt only", self.model)
            self.json_mode = False
            kwargs.pop("response_format")
            resp = self._client.chat.completions.create(**kwargs)
        return parse_json(resp.choices[0].message.content or "{}")


class AzureOpenAISummarizer(ChatSummarizer):
    def __init__(self, cfg: OpenAIConfig, secrets: SecretGetter, credential=None, prompt: str | None = None,
                 client=None):
        self.cfg = cfg
        if client is None:
            if cfg.auth == "aad":
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
                api_key: Any = get_bearer_token_provider(credential or DefaultAzureCredential(), COGNITIVE_SCOPE)
            else:
                api_key = require(secrets, "openai-key")
            client = make_client(cfg.endpoint, api_key, cfg.api_version, cfg.timeout_seconds)
        super().__init__(client, cfg.deployment, prompt, cfg.max_transcript_chars)


class AnthropicSummarizer(NotesWriter):
    """Claude via the Messages API (plain HTTPS; no extra SDK). Claude reads screenshots but not audio."""
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, prompt: str | None = None, max_chars: int = 350_000,
                 vision: bool = True, session=None, timeout: float = 300.0):
        import requests

        self._key, self.model, self.timeout = api_key, model, timeout
        self.prompt = prompt or default_prompt()
        self.max_chars, self.vision = max_chars, vision
        self._http = session or requests.Session()

    @staticmethod
    def _parts(user_content: Any) -> list[dict[str, Any]]:
        if isinstance(user_content, str):
            return [{"type": "text", "text": user_content}]
        out = []
        for part in user_content:
            if part.get("type") == "image_url":
                url = part["image_url"]["url"]
                media_type, _, data = url.removeprefix("data:").partition(";base64,")
                out.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            else:
                out.append({"type": "text", "text": part.get("text", "")})
        return out

    def _chat(self, user_content: Any) -> dict[str, Any]:
        from .transcribe import TransientError

        body = {"model": self.model, "max_tokens": 8000, "system": self.prompt + "\nReply with the JSON object only.",
                "messages": [{"role": "user", "content": self._parts(user_content)}]}
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            r = self._http.post(self.URL, json=body, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - network
            raise TransientError(f"Anthropic: {exc}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"Anthropic returned {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic returned {r.status_code}: {r.text[:500]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return parse_json(text)


def _bad_request(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) in (400, 422) or "BadRequest" in type(exc).__name__
