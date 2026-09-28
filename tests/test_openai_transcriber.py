from types import SimpleNamespace

import pytest

from meetcap.config import OpenAIConfig
from meetcap.transcribe import AzureOpenAITranscriber, _speaker_id, parse_openai_transcription


def test_speaker_ids():
    assert [_speaker_id(x) for x in ("A", "b", "speaker_0", 2, None, "")] == [1, 2, 1, 3, None, None]


def test_parse_diarized_whisper_and_text():
    diar = {"segments": [{"start": 1.0, "end": 2.5, "text": " Hi ", "speaker": "A"},
                         {"start": 3.0, "end": 4.0, "text": "Hello", "speaker": "B"}]}
    ps = parse_openai_transcription(diar, offset_ms=60_000)
    got = [(p.start_ms, p.duration_ms, p.text, p.speaker) for p in ps]
    assert got == [(61000, 1500, "Hi", 1), (63000, 1000, "Hello", 2)]
    whisper = {"segments": [{"start": 0.0, "end": 1.0, "text": "one"}]}
    assert parse_openai_transcription(whisper)[0].speaker is None
    text = {"text": "First point here. Second one! Third?"}
    ps = parse_openai_transcription(text, offset_ms=0, span_ms=30_000)
    assert [p.text for p in ps] == ["First point here.", "Second one!", "Third?"]
    assert ps[0].start_ms == 0 and 0 < ps[1].start_ms < ps[2].start_ms < 30_000


class BadRequest(Exception):
    status_code = 400


class FakeAudio:
    def __init__(self, supported):
        self.supported, self.calls = supported, []

    def create(self, file, **kw):
        self.calls.append(kw)
        if kw["response_format"] not in self.supported:
            raise BadRequest("unsupported response_format")
        if kw["response_format"] == "diarized_json":
            return SimpleNamespace(model_dump=lambda: {"segments": [{"start": 0, "end": 1, "text": "hey", "speaker": "A"}]})
        return {"text": "Just text."}


def _tx(supported, **cfg):
    audio = FakeAudio(supported)
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=audio))
    conf = OpenAIConfig(endpoint="https://x.openai.azure.com", transcribe_deployment="tx", **cfg)
    return AzureOpenAITranscriber(conf, lambda n: "k", client=client, language="en", phrases=["Acme"]), audio


def test_detect_format_falls_back(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    tr, audio = _tx({"json"})
    assert tr.detect_format(f) == "json"
    assert [c["response_format"] for c in audio.calls] == ["diarized_json", "verbose_json", "json"]
    assert audio.calls[0]["chunking_strategy"] == "auto" and "prompt" not in audio.calls[0]
    assert audio.calls[1]["timestamp_granularities"] == ["segment"] and "Acme" in audio.calls[1]["prompt"]


def test_transcribe_chunks_long_audio(sample_video, tmp_path):
    from meetcap import media

    ogg = media.extract_audio(sample_video, tmp_path / "a.ogg")  # ~12 s
    tr, audio = _tx({"diarized_json"}, transcribe_format="diarized_json")
    tr.CHUNK_SECONDS = {"diarized_json": 5, "verbose_json": 5, "json": 5}
    t = tr.transcribe(ogg)
    assert len(audio.calls) == 3                          # 0-5 s, 5-10 s, 10-12 s
    assert [p.start_ms for p in t.phrases] == [0, 5000, 10000] and len(t.raw) == 3


def test_every_format_rejected_is_explained(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    tr, _ = _tx(set())
    with pytest.raises(Exception, match="rejected every format"):
        tr.detect_format(f)
