import json
from importlib import resources
from types import SimpleNamespace

import pytest
import yaml

from meetcap.cli import build_parser
from meetcap.config import Config, ConfigError, _build, load_config, problems
from meetcap.models import Frame
from meetcap.summarize import AzureOpenAISummarizer, normalize_notes, split_text
from meetcap.config import OpenAIConfig


def test_example_config_loads_and_flags_placeholders(tmp_path):
    text = resources.files("meetcap").joinpath("config.example.yaml").read_text()
    p = tmp_path / "c.yaml"
    p.write_text(text)
    cfg = load_config(p)
    assert cfg.recording.obs.port == 4455 and cfg.azure.speech.api_version == "2025-10-15"
    assert (cfg.storage.backend, cfg.transcription.engine, cfg.summary.provider) == ("local", "local", "none")
    assert problems(cfg) == []                       # works out of the box: nothing to fill in


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="recording.obs"):
        _build(Config, yaml.safe_load("recording:\n  obs:\n    prot: 1\n"))


def test_filled_config_has_no_problems():
    cfg = _build(Config, {"storage": {"backend": "azure"}, "transcription": {"engine": "azure-speech"},
                          "summary": {"provider": "azure-openai"},
                          "azure": {"storage": {"account_url": "https://a.blob.core.windows.net"},
                                    "speech": {"endpoint": "https://s.cognitiveservices.azure.com"},
                                    "openai": {"endpoint": "https://o.openai.azure.com"}}})
    assert problems(cfg) == []


def test_old_azure_config_keeps_working(tmp_path):
    """A 0.8 config (Azure storage with a connection string, Azure OpenAI notes, Whisper on the Mac)."""
    p = tmp_path / "c.yaml"
    p.write_text("transcription:\n  engine: local\nazure:\n  storage:\n    auth: key\n    prefix: alex\n"
                 "  openai:\n    endpoint: https://o.openai.azure.com\n    deployment: gpt-4.1\n")
    cfg = load_config(p)
    assert (cfg.storage.backend, cfg.summary.provider, cfg.transcription.engine) == ("azure", "azure-openai", "local")
    assert cfg.blob_prefix == "alex" and problems(cfg) == []
    p.write_text("transcription:\n  engine: speech\n")
    assert load_config(p).transcription.engine == "azure-speech"
    p.write_text("transcription:\n  engine: openai\n")
    assert load_config(p).transcription.engine == "azure-openai"


def test_bad_choices_are_named():
    cfg = _build(Config, {"storage": {"backend": "s3"}, "transcription": {"engine": "whisperx"},
                          "summary": {"provider": "gemini"}})
    out = " ".join(problems(cfg))
    assert "storage.backend" in out and "transcription.engine" in out and "summary.provider" in out


class FakeCompletions:
    def __init__(self, fail_with_images=False):
        self.calls, self.fail_with_images = [], fail_with_images

    def create(self, **kw):
        self.calls.append(kw)
        content = kw["messages"][1]["content"]
        if self.fail_with_images and isinstance(content, list) and any(c["type"] == "image_url" for c in content):
            raise RuntimeError("model does not support images")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"title": "T", "summary": "S", "action_items": "not-a-list"})))])


def _summ(fail=False, max_chars=350_000):
    comp = FakeCompletions(fail)
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    cfg = OpenAIConfig(endpoint="https://o.openai.azure.com", deployment="gpt-4.1", max_transcript_chars=max_chars)
    return AzureOpenAISummarizer(cfg, secrets=lambda n: "k", client=client), comp


def test_summarizer_sends_frames_and_json_mode(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8fakejpeg")
    s, comp = _summ()
    notes = s.summarize("[00:00:01] S1: hello", {"app": "teams"}, [Frame(str(img), 61000)])
    call = comp.calls[0]
    assert call["model"] == "gpt-4.1" and call["response_format"] == {"type": "json_object"}
    parts = call["messages"][1]["content"]
    assert parts[1]["text"] == "Screenshot at 00:01:01:" and parts[2]["image_url"]["url"].startswith("data:image/jpeg")
    assert "JSON" in call["messages"][0]["content"]
    assert notes["title"] == "T" and notes["action_items"] == [] and notes["decisions"] == []


def test_summarizer_falls_back_to_text_only(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    s, comp = _summ(fail=True)
    assert s.summarize("[00:00:01] S1: hi", {}, [Frame(str(img), 0)])["title"] == "T"
    assert len(comp.calls) == 2 and isinstance(comp.calls[1]["messages"][1]["content"], str)


def test_summarizer_map_reduce_for_long_transcripts():
    s, comp = _summ(max_chars=50)
    text = "\n".join(f"[00:00:{i:02d}] S1: line number {i}" for i in range(10))
    s.summarize(text, {}, [])
    assert len(comp.calls) > 2  # several segment calls + one merge call
    assert "merge them" in comp.calls[-1]["messages"][1]["content"][0]["text"]


def test_split_text_respects_limit():
    chunks = split_text("a\n" * 100, 20)
    assert all(len(c) <= 20 for c in chunks) and "".join(chunks) == "a\n" * 100


def test_normalize_defaults():
    assert normalize_notes({})["title"] == "Untitled meeting"


def test_cli_parser_covers_commands():
    p = build_parser()
    for argv in (["init"], ["doctor", "--deep"], ["secret", "set", "speech-key"], ["process", "x.mp4", "--now"],
                 ["jobs", "--status", "failed"], ["retry"], ["mics", "--all"], ["setup-obs"], ["menubar"], ["run"],
                 ["config"], ["restart"], ["azure-setup"], ["azure-setup", "--no-verify"],
                 ["status"], ["start"], ["stop"], ["auto", "on"], ["pet", "on"], ["pet", "test"], ["pet", "list"],
                 ["pet", "koala"], ["obs-log"],
                 ["obs-log", "--crashes", "8", "--days", "3"],
                 ["notes"], ["notes", "--serve"], ["notes", "--local"], ["reader"], ["reader", "--revoke"],
                 ["reader", "--print", "--days", "30", "--yes", "--no-open"], ["setup"],
                 ["setup", "--no-open"]):
        assert p.parse_args(argv).fn
