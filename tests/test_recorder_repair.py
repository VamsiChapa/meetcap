import base64
import json
import struct

import pytest

from meetcap.config import Config
from meetcap import recorder as recorder_mod
from meetcap.obs_client import ObsConnectionError, ObsRequestError
from meetcap.recorder import MIC_INPUT, SCREEN_INPUT, SYSTEM_AUDIO_INPUT, ObsRecorder


def _bmp(value: int) -> str:
    px = bytes([value, value, value, 255]) * 4
    raw = b"BM" + struct.pack("<IHHI", 54 + len(px), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 32, 0,
                                                                              len(px), 0, 0, 0, 0) + px
    return "data:image/bmp;base64," + base64.b64encode(raw).decode()


class FakeObs:
    """OBS in its factory state: 'Untitled' profile and collection, one empty 'Scene'. The situation that
    produced the black, silent recording."""

    def __init__(self, screen=200, mic=0.03, displays=True):
        self.profiles, self.profile = ["Untitled"], "Untitled"
        self.collections, self.collection = ["Untitled"], "Untitled"
        self.scenes, self.program = {"Untitled": ["Scene"]}, "Scene"
        self.inputs, self.settings, self.tracks, self.params = {}, {}, {}, {}
        self.screen, self.mic, self.displays = screen, mic, displays
        self.recording = False
        self.alive, self.crash_on = True, set()

    def ping(self):
        return self.alive

    def close(self):
        pass

    def call(self, req, data=None):
        d = data or {}
        if not self.alive:
            raise ObsConnectionError("Connection refused")
        if req == getattr(self, "crash_once_on", None):
            self.crash_once_on = None
            raise ObsConnectionError("Connection refused")      # macOS made OBS quit & reopen
        if getattr(self, "screen_kills_obs", False) and SCREEN_INPUT in self.inputs:
            self.calls_with_screen = getattr(self, "calls_with_screen", 0) + 1
            if self.calls_with_screen > 1:            # survives the create + first check, then dies
                self.alive, self.calls_with_screen = False, 0
                raise ObsConnectionError("Connection refused")
        if req == "GetProfileList":
            return {"profiles": self.profiles, "currentProfileName": self.profile}
        if req == "CreateProfile":
            self.profiles.append(d["profileName"])
            self.profile = d["profileName"]
            return {}
        if req == "SetCurrentProfile":
            self.profile = d["profileName"]
            return {}
        if req == "SetProfileParameter":
            self.params[(d["parameterCategory"], d["parameterName"])] = d["parameterValue"]
            return {}
        if req == "GetVideoSettings":
            return {"baseWidth": 3024, "baseHeight": 1964}
        if req == "SetInputMute":
            self.muted = getattr(self, "muted", {})
            self.muted[d["inputName"]] = d["inputMuted"]
            return {}
        if req in ("SetVideoSettings", "SetSceneItemTransform"):
            return {}
        if req == "GetSceneItemId":
            return {"sceneItemId": 1}
        if req == "GetSceneItemTransform":
            return {"sceneItemTransform": {"sourceWidth": 3024, "sourceHeight": 1964}}
        if req == "GetSceneCollectionList":
            return {"sceneCollections": self.collections, "currentSceneCollectionName": self.collection}
        if req == "CreateSceneCollection":
            self.collections.append(d["sceneCollectionName"])
            self.collection = d["sceneCollectionName"]
            self.scenes[self.collection] = ["Scene"]
            return {}
        if req == "SetCurrentSceneCollection":
            self.collection = d["sceneCollectionName"]
            return {}
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": n} for n in self.scenes[self.collection]]}
        if req == "CreateScene":
            self.scenes[self.collection].append(d["sceneName"])
            return {}
        if req == "SetCurrentProgramScene":
            self.program = d["sceneName"]
            return {}
        if req == "GetInputList":
            return {"inputs": [{"inputName": n} for n in self.inputs]}
        if req == "CreateInput":
            self.inputs[d["inputName"]] = d["inputKind"]
            self.settings[d["inputName"]] = dict(d.get("inputSettings") or {})
            if d["inputName"] in self.crash_on:
                self.alive = False          # OBS dies a moment after the source starts
            return {}
        if req == "GetProfileParameter":
            return {"parameterValue": self.params.get((d["parameterCategory"], d["parameterName"]))}
        if req == "GetVersion":
            return {"obsVersion": "32.0.1", "obsWebSocketVersion": "5.6.2"}
        if req == "GetInputPropertiesListPropertyItems":   # what really happened: obs-websocket dies (strlen NULL)
            self.alive = False
            raise ObsConnectionError("Connection refused")
        if req == "GetInputSettings":
            return {"inputSettings": self.settings.get(d["inputName"], {})}
        if req == "SetInputSettings":
            self.settings.setdefault(d["inputName"], {}).update(d["inputSettings"])
            return {}
        if req == "GetSpecialInputs":
            return {"desktop1": None, "mic1": None}
        if req == "SetInputAudioTracks":
            self.tracks[d["inputName"]] = d["inputAudioTracks"]
            return {}
        if req == "GetRecordStatus":
            return {"outputActive": self.recording}
        if req == "GetSourceScreenshot":
            return {"imageData": _bmp(self.screen)}
        raise ObsRequestError(req, 204, "not faked")

    # event listener used by levels()
    def read_events(self, seconds, event_type=None):
        return [{"inputs": [{"inputName": MIC_INPUT, "inputLevelsMul": [[self.mic, self.mic, self.mic]]},
                            {"inputName": SYSTEM_AUDIO_INPUT, "inputLevelsMul": [[0.0, 0.0, 0.0]]}]}]


@pytest.fixture(autouse=True)
def _isolated_quarantine(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder_mod, "QUARANTINE_PATH", tmp_path / "obs_quarantine.json")
    monkeypatch.setattr(recorder_mod, "installed_obs_version", lambda p: "32.2.2")
    monkeypatch.setattr(recorder_mod, "display_uuids", lambda: ["UUID-1", "UUID-2"])
    monkeypatch.setattr(recorder_mod, "DIAG_DIR", tmp_path / "no-reports")


def _recorder(obs):
    return ObsRecorder(Config(), password=lambda: "pw", client_factory=lambda *a, **k: obs)


def test_factory_default_obs_is_repaired_before_recording():
    obs = FakeObs()
    _recorder(obs).ensure_ready()
    assert obs.profile == "MeetCap" and obs.collection == "MeetCap" and obs.program == "MeetCap"
    assert obs.inputs == {SCREEN_INPUT: "screen_capture", SYSTEM_AUDIO_INPUT: "sck_audio_capture",
                          MIC_INPUT: "coreaudio_input_capture"}
    assert obs.settings[SCREEN_INPUT]["display_uuid"] == "UUID-1"
    assert obs.tracks[MIC_INPUT]["2"] and not obs.tracks[MIC_INPUT]["3"]
    assert obs.tracks[SYSTEM_AUDIO_INPUT]["3"] and not obs.tracks[SYSTEM_AUDIO_INPUT]["2"]
    assert obs.params[("SimpleOutput", "RecTracks")] == "7"
    assert obs.params[("Output", "FilenameFormatting")].startswith("meetcap_")


def test_repair_is_idempotent():
    obs = FakeObs()
    rec = _recorder(obs)
    rec.ensure_ready()
    before = (list(obs.profiles), list(obs.collections), dict(obs.inputs))
    rec.ensure_ready()
    assert (obs.profiles, obs.collections, obs.inputs) == before


def test_capture_check_finds_black_screen_and_deaf_mic():
    obs = FakeObs(screen=0, mic=0.0)
    rec = _recorder(obs)
    rec.ensure_ready()
    problems = rec.check_capture(0)
    assert len(problems) == 2
    assert "black screen" in problems[0] and "Screen & System Audio Recording" in problems[0]
    assert "microphone" in problems[1] and "Microphone" in problems[1]


def test_capture_check_passes_when_screen_and_mic_work():
    obs = FakeObs(screen=180, mic=0.004)   # a quiet room still has a noise floor
    rec = _recorder(obs)
    rec.ensure_ready()
    assert rec.check_capture(0) == []


def test_missing_screen_permission_does_not_block_audio_recording(monkeypatch):
    monkeypatch.setattr(recorder_mod, "display_uuids", lambda: [])
    obs = FakeObs(screen=0)
    _recorder(obs).ensure_ready()          # still sets up the audio sources; the capture check reports the screen
    assert MIC_INPUT in obs.inputs and SYSTEM_AUDIO_INPUT in obs.inputs


def test_setup_survives_obs_closing_midway(monkeypatch, tmp_path):
    obs = FakeObs()
    obs.crash_once_on = "GetSceneCollectionList"   # closes before any source is touched (permission Quit & Reopen)
    monkeypatch.setattr(recorder_mod, "WEBSOCKET_CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(recorder_mod, "installed_obs_version", lambda p: "32.0.1")
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(ObsRecorder, "launch", lambda self, timeout=None: None)
    monkeypatch.setattr(ObsRecorder, "quit_obs", lambda self, timeout=30.0: None)
    cfg = Config()
    cfg.recording.output_dir = str(tmp_path / "rec")
    rec = ObsRecorder(cfg, password=lambda: "pw", client_factory=lambda *a, **k: obs)
    seen = []
    steps = rec.setup(lambda pw: None, on_step=seen.append)
    assert seen == list(steps)                                   # printed live, in order
    assert any("OBS closed during setup" in x for x in steps)
    assert steps[-2] == "OBS sees your screen and hears your microphone"
    assert obs.inputs.keys() == {SCREEN_INPUT, SYSTEM_AUDIO_INPUT, MIC_INPUT}


def test_setup_gives_up_with_plain_advice(monkeypatch):
    rec = _recorder(FakeObs())
    monkeypatch.setattr(ObsRecorder, "launch", lambda self, timeout=None: None)
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda s: None)

    def always_closed():
        raise ObsConnectionError("Connection refused")   # dies before any source is touched

    with pytest.raises(recorder_mod.RecorderError, match="Open OBS yourself once"):
        rec._safely(always_closed, [], "32.2.2")



def _setup_with_crashes(monkeypatch, tmp_path, crash_on):
    obs = FakeObs()
    obs.crash_on = set(crash_on)
    monkeypatch.setattr(recorder_mod, "WEBSOCKET_CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(ObsRecorder, "launch", lambda self, timeout=None: setattr(obs, "alive", True))
    monkeypatch.setattr(ObsRecorder, "quit_obs", lambda self, timeout=30.0: None)
    monkeypatch.setattr(recorder_mod, "remove_sources_from_collection",
                        lambda coll, names: [obs.inputs.pop(n) for n in names if n in obs.inputs])
    cfg = Config()
    cfg.recording.output_dir = str(tmp_path / "rec")
    rec = ObsRecorder(cfg, password=lambda: "pw", client_factory=lambda *a, **k: obs)
    return rec, obs, rec.setup(lambda pw: None)


def test_screen_capture_crash_leaves_video_out_but_keeps_audio(monkeypatch, tmp_path):
    rec, obs, steps = _setup_with_crashes(monkeypatch, tmp_path, {SCREEN_INPUT})
    warnings = [x for x in steps if x.startswith("WARNING: ")]
    assert len(warnings) == 1 and "records audio only" in warnings[0]
    assert not any(x.startswith("PROBLEM: ") for x in steps)
    assert set(obs.inputs) == {MIC_INPUT, SYSTEM_AUDIO_INPUT}
    assert rec.quarantined == {SCREEN_INPUT} and not rec.video_available
    rec.ensure_ready()                                    # before a meeting: must not re-add the crasher
    assert SCREEN_INPUT not in obs.inputs and obs.alive


def test_audio_source_crash_falls_back_to_screen_audio(monkeypatch, tmp_path):
    rec, obs, steps = _setup_with_crashes(monkeypatch, tmp_path, {SYSTEM_AUDIO_INPUT})
    assert any("takes the meeting audio from the screen capture" in x for x in steps)
    assert set(obs.inputs) == {MIC_INPUT, SCREEN_INPUT} and rec.video_available
    assert obs.muted[SCREEN_INPUT] is False and obs.tracks[SCREEN_INPUT]["3"] is True


def test_both_capture_sources_crashing_is_a_problem(monkeypatch, tmp_path):
    rec, obs, steps = _setup_with_crashes(monkeypatch, tmp_path, {SYSTEM_AUDIO_INPUT, SCREEN_INPUT})
    assert any(x.startswith("PROBLEM: ") and "not the other people" in x for x in steps)
    assert set(obs.inputs) == {MIC_INPUT}


def test_new_obs_version_retries_quarantined_sources(tmp_path):
    recorder_mod.save_quarantine({SCREEN_INPUT}, "32.2.2")
    assert recorder_mod.load_quarantine("32.2.2") == {SCREEN_INPUT}
    assert recorder_mod.load_quarantine("32.3.0") == set()


def test_remove_sources_from_collection_file(monkeypatch, tmp_path):
    f = tmp_path / "MeetCap.json"
    f.write_text(json.dumps({"sources": [
        {"name": SCREEN_INPUT, "id": "screen_capture"}, {"name": MIC_INPUT, "id": "coreaudio_input_capture"},
        {"name": "MeetCap", "id": "scene", "settings": {"items": [{"name": SCREEN_INPUT}, {"name": MIC_INPUT}]}}]}))
    monkeypatch.setattr(recorder_mod, "collection_file", lambda c: f)
    assert recorder_mod.remove_sources_from_collection("MeetCap", [SCREEN_INPUT]) == [SCREEN_INPUT]
    data = json.loads(f.read_text())
    assert [x["name"] for x in data["sources"]] == [MIC_INPUT, "MeetCap"]
    assert data["sources"][1]["settings"]["items"] == [{"name": MIC_INPUT}]


def test_real_world_delayed_screen_crash_is_blamed_on_screen(monkeypatch, tmp_path):
    """What happened on the M3 / macOS 26.6 / OBS 32.2.2: audio sources fine, OBS dies a moment after the
    screen source starts, and again on every relaunch while that source is in the saved scene."""
    obs = FakeObs()
    obs.screen_kills_obs = True
    monkeypatch.setattr(recorder_mod, "WEBSOCKET_CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(ObsRecorder, "launch", lambda self, timeout=None: setattr(obs, "alive", True))
    monkeypatch.setattr(ObsRecorder, "quit_obs", lambda self, timeout=30.0: None)
    monkeypatch.setattr(recorder_mod, "remove_sources_from_collection",
                        lambda coll, names: [obs.inputs.pop(n) for n in names if n in obs.inputs])
    cfg = Config()
    cfg.recording.output_dir = str(tmp_path / "rec")
    rec = ObsRecorder(cfg, password=lambda: "pw", client_factory=lambda *a, **k: obs)
    steps = rec.setup(lambda pw: None)
    assert any("records audio only" in x for x in steps)
    assert not any(x.startswith("PROBLEM: ") for x in steps)
    assert SCREEN_INPUT not in obs.inputs and SYSTEM_AUDIO_INPUT in obs.inputs
    assert rec.quarantined == {SCREEN_INPUT}
    assert "OBS hears your microphone and will record the meeting audio" in steps[-2]


def test_crash_summary_finds_thread_plugin_and_quit(tmp_path):
    import json as _json

    from meetcap.recorder import crash_summary
    body = {"captureTime": "2026-09-28 01:25:19 -0500", "procLaunch": "2026-09-28 01:24:45 -0500",
            "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV", "subtype": "KERN_INVALID_ADDRESS at 0x10"},
            "termination": {"indicator": "Segmentation fault: 11"}, "faultingThread": 1,
            "usedImages": [{"name": "libobs", "path": "/Applications/OBS.app/Contents/Frameworks/libobs.framework/libobs"},
                           {"name": "mac-capture", "path": "/Applications/OBS.app/Contents/PlugIns/mac-capture.plugin/x"},
                           {"name": "ScreenCaptureKit", "path": "/System/Library/Frameworks/ScreenCaptureKit.framework/x"}],
            "threads": [{"frames": []}, {"triggered": True, "queue": "com.apple.main-thread", "frames": [
                {"imageIndex": 2, "symbol": "-[SCStream stopCaptureWithCompletionHandler:]"},
                {"imageIndex": 1, "symbol": "screen_capture_destroy"},
                {"imageIndex": 0, "symbol": "obs_shutdown"}, {"imageIndex": 9, "imageOffset": 4096}]}]}
    ips = tmp_path / "OBS-2026-09-28-012528.ips"
    ips.write_text(_json.dumps({"app_name": "OBS"}) + "\n" + _json.dumps(body))
    out = crash_summary(ips)
    assert "EXC_BAD_ACCESS SIGSEGV" in out and "crashed thread 1 (com.apple.main-thread)" in out
    assert "screen_capture_destroy" in out and "0x1000" in out
    assert "OBS plugin involved: mac-capture" in out and "Apple framework involved: ScreenCaptureKit" in out
    assert "while quitting" in out
    ips.write_text("not json at all")
    assert crash_summary(ips) == "not json at all"


def _ips(tmp_path, name, images, frames):
    import json as _json
    body = {"exception": {"type": "EXC_BAD_ACCESS"}, "faultingThread": 0,
            "usedImages": [{"name": n, "path": p} for n, p in images],
            "threads": [{"triggered": True, "frames": [{"imageIndex": i, "symbol": sym} for i, sym in frames]}]}
    f = tmp_path / name
    f.write_text(_json.dumps({"app_name": "OBS"}) + "\n" + _json.dumps(body))
    return f


APP = "/Applications/OBS.app/Contents"
WEBSOCKET = ([("libsystem_platform.dylib", "/usr/lib/system/x"), ("obs-websocket", f"{APP}/PlugIns/obs-websocket.plugin/x")],
             [(0, "_platform_strlen"), (1, None)])
AUDIO = ([("libobs", f"{APP}/Frameworks/libobs"), ("mac-capture", f"{APP}/PlugIns/mac-capture.plugin/x"),
          ("CoreAudio", "/System/Library/Frameworks/CoreAudio.framework/x")],
         [(0, "copy_audio_data"), (0, "obs_source_output_audio"), (1, None), (2, "HALC_ProxyIOContext::IOWorkLoop()")])
SCREEN = ([("mac-capture", f"{APP}/PlugIns/mac-capture.plugin/x"),
           ("ScreenCaptureKit", "/System/Library/Frameworks/ScreenCaptureKit.framework/x")],
          [(1, "-[SCStream start]"), (0, None)])


def test_crash_kind_reads_the_crashed_thread(tmp_path):
    assert recorder_mod.crash_kind(_ips(tmp_path, "a.ips", *WEBSOCKET)) == "websocket"
    assert recorder_mod.crash_kind(_ips(tmp_path, "b.ips", *AUDIO)) == "audio-device"
    assert recorder_mod.crash_kind(_ips(tmp_path, "c.ips", *SCREEN)) == "source"
    (tmp_path / "d.ips").write_text("garbage")
    assert recorder_mod.crash_kind(tmp_path / "d.ips") == "unknown"
    assert "obs-websocket crashed answering a request" in recorder_mod.crash_summary(tmp_path / "a.ips")
    assert "microphone input crashed" in recorder_mod.crash_summary(tmp_path / "b.ips")


@pytest.mark.parametrize("report, said", [(WEBSOCKET, "websocket code"), (AUDIO, "microphone input code")])
def test_obs_own_crash_does_not_switch_the_screen_off(monkeypatch, tmp_path, report, said):
    """The night-of-28-September case: OBS died right after the screen source was added, but the report
    shows OBS's own code crashed. The screen must stay in."""
    obs = FakeObs()
    obs.crash_on = {SCREEN_INPUT}
    path = _ips(tmp_path, "r.ips", *report)

    def report_once(since, wait=None):
        obs.crash_on.discard(SCREEN_INPUT)       # OBS's bug is a race: the retry goes through
        return path
    monkeypatch.setattr(ObsRecorder, "_crash_report_since", staticmethod(report_once))
    monkeypatch.setattr(recorder_mod, "WEBSOCKET_CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(ObsRecorder, "launch", lambda self, timeout=None: setattr(obs, "alive", True))
    monkeypatch.setattr(ObsRecorder, "quit_obs", lambda self, timeout=30.0: None)
    cfg = Config()
    cfg.recording.output_dir = str(tmp_path / "rec")
    rec = ObsRecorder(cfg, password=lambda: "pw", client_factory=lambda *a, **k: obs)
    steps = rec.setup(lambda pw: None)
    assert any(said in x and "not in MeetCap Screen" in x for x in steps)
    assert rec.quarantined == set() and rec.video_available and SCREEN_INPUT in obs.inputs
    assert obs.settings[SCREEN_INPUT]["display_uuid"] == "UUID-1"
    assert steps[-2] == "OBS sees your screen and hears your microphone"


def test_close_after_recording_skips_the_crashing_quit(monkeypatch):
    ran, cleared = [], []
    running = iter([True, False])
    monkeypatch.setattr(recorder_mod, "obs_running", lambda: next(running, False))
    monkeypatch.setattr(recorder_mod.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(recorder_mod, "clear_crash_sentinels", lambda: cleared.append(1))
    obs = FakeObs()
    _recorder(obs).close_after_recording()
    assert ran == [["pkill", "-KILL", "-x", "OBS"]] and cleared == [1]
    obs.recording, ran[:] = True, []
    monkeypatch.setattr(recorder_mod, "obs_running", lambda: True)
    _recorder(obs).close_after_recording()                 # a new recording started: leave OBS alone
    assert ran == []
