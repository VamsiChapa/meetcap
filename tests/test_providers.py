import json
import os
import time
from types import SimpleNamespace

import pytest

from meetcap import providers
from meetcap.config import Config
from meetcap.jobs import JobStore
from meetcap.models import Frame, Job, Status
from meetcap.pipeline import Pipeline
from meetcap.storage import LocalStore, StorageError
from meetcap.summarize import AnthropicSummarizer, ChatSummarizer, parse_json
from meetcap.transcribe import OpenAICompatTranscriber, TransientError
from tests.test_pipeline_agent import FakeTranscriber

KEYS = {"groq-key": "gk", "openrouter-key": "ok", "anthropic-key": "ak", "openai-api-key": "oa", "openai-key": "az"}


def _cfg(**over):
    cfg = Config()
    for dotted, value in over.items():
        node, *path = dotted.split("__")
        obj = getattr(cfg, node)
        for part in path[:-1]:
            obj = getattr(obj, part)
        setattr(obj, path[-1], value)
    return cfg


@pytest.mark.parametrize("engine, cls, model", [
    ("groq", OpenAICompatTranscriber, "whisper-large-v3-turbo"),
    ("openrouter", OpenAICompatTranscriber, "openai/whisper-large-v3"),
    ("openai", OpenAICompatTranscriber, "whisper-1"),
])
def test_cloud_transcribers(engine, cls, model):
    t = providers.make_transcriber(_cfg(transcription__engine=engine), KEYS.get)
    assert isinstance(t, cls) and t.model == model and t.fmt == "verbose_json"
    assert str(t._client.base_url).rstrip("/") == providers.PRESETS[engine].base_url
    assert t._client.api_key == KEYS[providers.PRESETS[engine].secret]


def test_groq_chunks_fit_the_free_tier_and_vocabulary_is_trimmed():
    cfg = _cfg(transcription__engine="groq")
    cfg.azure.speech.phrase_list = ["word"] * 400
    t = providers.make_transcriber(cfg, KEYS.get)
    assert t.chunk_seconds == 20 * 60 and len(t.prompt) <= 800


def test_off_means_none_and_missing_keys_say_how_to_fix():
    assert providers.make_transcriber(_cfg(transcription__engine="none"), KEYS.get) is None
    assert providers.make_summarizer(_cfg(summary__provider="none"), KEYS.get) is None
    with pytest.raises(RuntimeError, match="meetcap secret set groq-key"):
        providers.make_transcriber(_cfg(transcription__engine="groq"), lambda n: None)


@pytest.mark.parametrize("provider, model, vision", [
    ("groq", "openai/gpt-oss-120b", False), ("openrouter", "openrouter/free", False),
    ("openai", "gpt-5-mini", True), ("ollama", "llama3.2", False)])
def test_chat_summarizers(provider, model, vision):
    s = providers.make_summarizer(_cfg(summary__provider=provider), KEYS.get)
    assert isinstance(s, ChatSummarizer) and s.model == model and s.vision is vision
    assert s.max_chars == providers.PRESETS[provider].max_chars


def test_claude_and_overrides():
    s = providers.make_summarizer(_cfg(summary__provider="anthropic", summary__model="claude-sonnet-5",
                                       summary__screenshots=False), KEYS.get)
    assert isinstance(s, AnthropicSummarizer) and s.model == "claude-sonnet-5" and s.vision is False
    s = providers.make_summarizer(_cfg(summary__provider="ollama", summary__base_url="http://10.0.0.5:11434/v1"), KEYS.get)
    assert str(s._client.base_url).startswith("http://10.0.0.5:11434/v1")


class BadRequest(Exception):
    status_code = 400


class Completions:
    def __init__(self, reply, reject_json=False):
        self.calls, self.reply, self.reject_json = [], reply, reject_json

    def create(self, **kw):
        self.calls.append(kw)
        if self.reject_json and "response_format" in kw:
            raise BadRequest("response_format not supported")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


def test_model_without_json_mode_still_works():
    comp = Completions('Here you go:\n```json\n{"title": "Weekly sync", "summary": "Fine."}\n```', reject_json=True)
    s = ChatSummarizer(SimpleNamespace(chat=SimpleNamespace(completions=comp)), "some/free-model", "prompt")
    notes = s.summarize("[00:00:01] S1: hello", {}, [])
    assert notes["title"] == "Weekly sync" and len(comp.calls) == 2 and "response_format" not in comp.calls[1]
    s.summarize("[00:00:01] S1: again", {}, [])
    assert len(comp.calls) == 3                     # remembered: no second failed attempt
    assert parse_json('{"a": 1}') == {"a": 1} and parse_json('ok {"a": {"b": 2}} done') == {"a": {"b": 2}}


def test_text_only_models_get_no_screenshots(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8jpeg")
    comp = Completions('{"title": "T"}')
    s = ChatSummarizer(SimpleNamespace(chat=SimpleNamespace(completions=comp)), "m", "p", vision=False)
    s.summarize("[00:00:01] S1: hi", {}, [Frame(str(img), 1000)])
    assert len(comp.calls[0]["messages"][1]["content"]) == 1    # just the text part


class Session:
    def __init__(self, status=200, text='{"title": "Claude notes", "summary": "S"}'):
        self.status, self.text, self.posts = status, text, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json, headers))
        body = {"content": [{"type": "text", "text": self.text}]}
        return SimpleNamespace(status_code=self.status, text="err", json=lambda: body)


def test_claude_request_shape(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8jpeg")
    sess = Session()
    s = AnthropicSummarizer("ak", "claude-haiku-4-5-20251001", "prompt", session=sess)
    notes = s.summarize("[00:00:01] S1: hi", {"app": "teams"}, [Frame(str(img), 61000)])
    url, body, headers = sess.posts[0]
    assert url.endswith("/v1/messages") and headers["x-api-key"] == "ak" and headers["anthropic-version"]
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text" and parts[1]["text"] == "Screenshot at 00:01:01:"
    assert parts[2] == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": parts[2]["source"]["data"]}}
    assert notes["title"] == "Claude notes"
    with pytest.raises(TransientError):
        AnthropicSummarizer("ak", "m", "p", session=Session(status=429))._chat("x")


# -- local folder storage, and the pipeline with transcription / notes switched off ---------------------------
def test_local_store_links_the_recording_and_refuses_escapes(tmp_path):
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x" * 1000)
    store = LocalStore(tmp_path / "MeetCap")
    r = store.upload_file(src, "2026/09/28/a/recording.mp4")
    dst = tmp_path / "MeetCap/2026/09/28/a/recording.mp4"
    assert r.size == 1000 and os.stat(dst).st_ino == os.stat(src).st_ino   # hard link: no second copy
    src.unlink()
    assert dst.read_bytes() == b"x" * 1000 and store.size("2026/09/28/a/recording.mp4") == 1000
    store.upload_text("hi", "2026/09/28/a/notes.md")
    assert store.list_names("2026/") == ["2026/09/28/a/notes.md", "2026/09/28/a/recording.mp4"]
    assert store.download_range("2026/09/28/a/notes.md", 1, 5) == b"i"
    with pytest.raises(StorageError):
        store.upload_text("x", "../../escape.txt")


def _local_pipeline(tmp_path, transcriber, summarizer):
    cfg = Config()
    cfg.recording.output_dir = str(tmp_path / "rec")
    cfg.storage.local_path = str(tmp_path / "MeetCap")
    cfg.detection.min_meeting_seconds = 5
    store = JobStore(tmp_path / "jobs.sqlite3")
    opened = []
    p = Pipeline(cfg, store, providers.make_store(cfg, KEYS.get), transcriber, summarizer,
                 notifier=lambda *a, **k: None, work_root=tmp_path / "work", on_ready=opened.append)
    return p, store, cfg, opened


def test_transcript_only_meeting_in_a_local_folder(sample_video, tmp_path):
    p, store, cfg, opened = _local_pipeline(tmp_path, FakeTranscriber(), None)
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=time.time() - 60, ended_at=time.time(),
                              app="com.microsoft.teams2", data={"owned": True})))
    assert job.status == Status.DONE, job.last_error
    folder = cfg.local_root / job.blob_prefix
    notes = json.loads((folder / "notes.json").read_text())
    assert notes["title"].startswith("Teams meeting, ") and notes["summary"] == ""
    assert (folder / "transcript.json").exists() and (folder / "audio.ogg").exists() and (folder / "audio.m4a").exists()
    assert (folder / "recording.mp4").exists() and not sample_video.exists()          # moved in, original gone
    assert (cfg.local_root / "Meeting Notes.html").exists() and (folder / "reader.js").exists()
    index = (cfg.local_root / "meetcap-index.js").read_text()
    assert index.startswith("window.MEETCAP_INDEX = ") and job.id in index
    assert opened and opened[0].id == job.id                                          # the reader opens


def test_no_transcription_still_keeps_the_meeting(sample_video, tmp_path):
    p, store, cfg, _ = _local_pipeline(tmp_path, None, None)
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=time.time() - 60, ended_at=time.time(),
                              app="manual", data={"owned": True})))
    assert job.status == Status.DONE, job.last_error
    notes = json.loads((cfg.local_root / job.blob_prefix / "notes.json").read_text())
    assert "Transcription is off" in notes["summary"] and notes["title"].startswith("Recorded meeting, ")
