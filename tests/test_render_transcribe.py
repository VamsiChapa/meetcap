import json

from meetcap import render
from meetcap.config import SpeechConfig
from meetcap.models import Phrase, Transcript
from meetcap.transcribe import AzureFastTranscriber, TransientError, parse_phrases

RAW = {
    "durationMilliseconds": 65000,
    "combinedPhrases": [{"channel": 0, "text": "..."}],
    "phrases": [
        {"channel": 0, "speaker": 1, "offsetMilliseconds": 960, "durationMilliseconds": 640, "text": "Good afternoon.",
         "locale": "en-US", "confidence": 0.93},
        {"channel": 0, "speaker": 1, "offsetMilliseconds": 1700, "durationMilliseconds": 900, "text": "This is Sam.",
         "locale": "en-US", "confidence": 0.93},
        {"channel": 0, "speaker": 2, "offsetMilliseconds": 61000, "durationMilliseconds": 2000, "text": "Hi Sam, Mary here.",
         "locale": "en-US", "confidence": 0.9},
        {"channel": 0, "speaker": 2, "offsetMilliseconds": 64000, "durationMilliseconds": 10, "text": "  "},
    ],
}


def test_parse_phrases_with_offset():
    ps = parse_phrases(RAW, offset_ms=1000)
    assert len(ps) == 3  # blank phrase dropped
    assert ps[0].start_ms == 1960 and ps[0].speaker == 1 and ps[2].text == "Hi Sam, Mary here."


def test_turns_and_llm_text():
    t = Transcript(65000, parse_phrases(RAW))
    text = render.transcript_for_llm(t)
    assert text.splitlines() == ["[00:00:00] S1: Good afternoon. This is Sam.", "[00:01:01] S2: Hi Sam, Mary here."]


def test_markdown_and_vtt_use_names():
    t = Transcript(65000, parse_phrases(RAW))
    names = render.speaker_names({"speakers": [{"label": "S2", "name": "Mary"}, {"label": "S1", "name": None}]})
    md = render.transcript_markdown(t, {"title": "Loan call", "app": "teams", "started_at": 0}, names)
    assert "**[00:01:01] Mary (S2):** Hi Sam, Mary here." in md
    vtt = render.transcript_vtt(t, names)
    assert vtt.startswith("WEBVTT") and "00:00:00.960 --> 00:00:01.600" in vtt and "<v Mary (S2)>" in vtt


def test_notes_markdown_sections():
    notes = {"title": "Pilot scope", "summary": "We agreed the scope.",
             "action_items": [{"owner": "Alex", "task": "Send SOW | v2", "due": "Fri", "timestamp": "00:10:00"}],
             "decisions": ["Go with Azure"], "key_points": [], "open_questions": ["Budget?"], "risks": [],
             "on_screen": [{"timestamp": "00:05:00", "what": "Architecture slide"}], "key_moments": [],
             "speakers": [{"label": "S1", "name": "Alex"}]}
    md = render.notes_markdown(notes, {"app": "teams", "duration_s": 600, "started_at": 0}, {"Audio": "x/audio.ogg"})
    assert md.startswith("# Pilot scope")
    assert "| Alex | Send SOW \\| v2 | Fri | 00:10:00 |" in md
    assert "## Shown on screen" in md and "`00:05:00` Architecture slide" in md
    assert "## Key points" not in md  # empty sections are omitted


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, json.dumps(body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


def _transcriber(resp):
    cfg = SpeechConfig(endpoint="https://res.cognitiveservices.azure.com/", locales=["en-US"], max_speakers=6,
                       phrase_list=["Acme"])
    return AzureFastTranscriber(cfg, secrets=lambda n: "k", session=_Session(resp))


def test_definition_and_request_shape(tmp_path):
    tr = _transcriber(_Resp(200, RAW))
    d = tr.definition()
    assert d["diarization"] == {"enabled": True, "maxSpeakers": 6}
    assert d["phraseList"] == {"phrases": ["Acme"]} and d["locales"] == ["en-US"]
    assert tr.url == ("https://res.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe"
                      "?api-version=2025-10-15")
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    raw = tr._post(audio)
    url, kw = tr._http.calls[0]
    assert kw["headers"] == {"Ocp-Apim-Subscription-Key": "k"}
    assert json.loads(kw["data"]["definition"])["diarization"]["maxSpeakers"] == 6
    assert raw["durationMilliseconds"] == 65000


def test_throttling_is_transient(tmp_path):
    tr = _transcriber(_Resp(429, {"error": "slow down"}))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    try:
        tr._post(audio)
    except TransientError:
        return
    raise AssertionError("expected TransientError")


def test_transcript_roundtrip():
    t = Transcript(10, [Phrase(1, 2, "hi", 1, "en-US")], [{"a": 1}])
    assert Transcript.from_dict(json.loads(json.dumps(t.to_dict()))) == t
