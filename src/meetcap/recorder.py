"""Recording backend: OBS Studio driven over obs-websocket.

OBS does the capture (ScreenCaptureKit video + system audio, Core Audio mic) and owns the macOS
Screen & System Audio Recording and Microphone permissions. meetcap never touches audio itself.
"""
from __future__ import annotations

import json
import logging
import plistlib
import secrets
import subprocess
import time
from pathlib import Path
from typing import Callable, Protocol

from .config import STATE_DIR, Config, expand
from .obs_client import (OUTPUT_NOT_RUNNING, OUTPUT_RUNNING, ObsClient, ObsConnectionError,
                         ObsRequestError)

log = logging.getLogger(__name__)

SCREEN_INPUT = "MeetCap Screen"
SYSTEM_AUDIO_INPUT = "MeetCap System Audio"
MIC_INPUT = "MeetCap Mic"
OBS_SUPPORT = Path.home() / "Library" / "Application Support" / "obs-studio"
WEBSOCKET_CONFIG = OBS_SUPPORT / "plugin_config" / "obs-websocket" / "config.json"
OBS_MIN_VERSION = (30, 2)            # hybrid MP4 + websocket config.json
FILENAME_PREFIX = "meetcap_"         # orphan scan only adopts files that start with this
FILENAME_FORMAT = FILENAME_PREFIX + "%CCYY-%MM-%DD_%hh-%mm-%ss"


class RecorderError(RuntimeError):
    pass


class SourceCrashed(RecorderError):
    """OBS died right after a source was added or configured."""

    def __init__(self, name: str):
        super().__init__(f"OBS crashed right after adding '{name}'")
        self.name = name


QUARANTINE_PATH = STATE_DIR / "obs_quarantine.json"
FRIENDLY = {SCREEN_INPUT: "screen capture", SYSTEM_AUDIO_INPUT: "meeting audio capture", MIC_INPUT: "microphone"}
SETTLE_SECONDS = 4.0  # OBS's screen/audio capture crashes, when it does, within a second or two of starting


def crash_advice(name: str, bad: set[str]) -> tuple[str, str]:
    """(WARNING|PROBLEM, what it means for recordings) after `name` crashed OBS."""
    upgrade = "An OBS update may fix it: `brew upgrade --cask obs`, then `meetcap setup-obs`."
    if name == MIC_INPUT:
        return "PROBLEM", f"OBS crashes when it opens your microphone, so your own voice can't be recorded. {upgrade}"
    if SCREEN_INPUT in bad and SYSTEM_AUDIO_INPUT in bad:
        return "PROBLEM", ("OBS crashes with both screen capture and meeting-audio capture on this Mac, so it can "
                           f"only record your microphone, not the other people. {upgrade}")
    if name == SCREEN_INPUT:
        return "WARNING", ("OBS crashes when capturing the screen on this Mac, so meetcap leaves video out and "
                           f"records audio only (your voice and the meeting). {upgrade}")
    return "WARNING", ("OBS crashes with its separate meeting-audio source here, so meetcap takes the meeting "
                       f"audio from the screen capture instead. {upgrade}")


def load_quarantine(obs_version: str | None) -> set[str]:
    """Sources that crashed this OBS version on this Mac. A different OBS version gets a fresh try."""
    try:
        data = json.loads(QUARANTINE_PATH.read_text())
    except (OSError, ValueError):
        return set()
    if obs_version and data.get("obs_version") != obs_version:
        return set()
    return set(data.get("inputs", []))


def save_quarantine(names: set[str], obs_version: str | None) -> None:
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUARANTINE_PATH.write_text(json.dumps({"obs_version": obs_version, "inputs": sorted(names),
                                           "updated": time.time()}, indent=2))


def collection_file(collection: str) -> Path:
    return OBS_SUPPORT / "basic" / "scenes" / f"{collection}.json"


def remove_sources_from_collection(collection: str, names: list[str]) -> list[str]:
    """Edit OBS's saved scene collection (OBS must be closed) to drop sources by name. Returns what was removed."""
    path = collection_file(collection)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    drop = set(names)
    sources = data.get("sources", [])
    removed = [x.get("name") for x in sources if x.get("name") in drop]
    data["sources"] = [x for x in sources if x.get("name") not in drop]
    for src in data["sources"]:
        settings = src.get("settings") or {}
        if isinstance(settings.get("items"), list):
            settings["items"] = [i for i in settings["items"] if i.get("name") not in drop]
    if removed:
        path.write_text(json.dumps(data, indent=4))
    return removed


def latest_obs_log() -> Path | None:
    logs = sorted((OBS_SUPPORT / "logs").glob("*.txt"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


DIAG_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"


def obs_crashes(days: float = 7, limit: int = 5) -> list[Path]:
    """OBS crash reports (OBS's own and macOS's), newest first."""
    found = [*(OBS_SUPPORT / "crashes").glob("*.txt"), *DIAG_DIR.glob("OBS*.ips")]
    recent = [p for p in found if time.time() - p.stat().st_mtime < days * 86400]
    return sorted(recent, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def latest_obs_crash() -> Path | None:
    """The newest OBS crash report from the last hour."""
    found = obs_crashes(days=1 / 24, limit=1)
    return found[0] if found else None


_QUITTING = ("terminate", "shutdown", "obs_shutdown", "exit", "__cxa_finalize", "~OBSApp", "~OBSBasic", "closeEvent")
CRASH_REPORT_WAIT = 20.0     # macOS writes the .ips report ~10 s after the crash


def _read_ips(path: Path) -> dict | None:
    if path.suffix != ".ips":
        return None
    _, _, body = path.read_text(errors="replace").partition("\n")
    try:
        return json.loads(body)
    except ValueError:
        return None


def _crashed_frames(d: dict, frames: int = 14) -> tuple[int, dict, list[tuple[str, str, str]]]:
    """(thread index, thread, [(image name, image path, symbol)]) for the thread that crashed."""
    images, threads = d.get("usedImages") or [], d.get("threads") or []
    idx = d.get("faultingThread", 0)
    if isinstance(idx, int) and idx < len(threads):
        thread = threads[idx]
    else:
        thread = next((t for t in threads if t.get("triggered")), {})
    out = []
    for f in (thread.get("frames") or [])[:frames]:
        i = f.get("imageIndex", -1)
        img = images[i] if isinstance(i, int) and 0 <= i < len(images) else {}
        name = img.get("name") or Path(img.get("path", "?")).name
        out.append((name, img.get("path", ""), f.get("symbol") or hex(int(f.get("imageOffset", 0)))))
    return idx, thread, out


def crash_kind(path: Path) -> str:
    """What crashed OBS, from its crash report:
    "websocket"    - obs-websocket answering a request (the request is at fault, not a source)
    "audio-device" - OBS's CoreAudio input handing audio over (a race in OBS when devices reset or it quits)
    "source"       - code inside a capture plugin
    "unknown"      - no readable report
    """
    d = _read_ips(path)
    if not d:
        return "unknown"
    _, _, frames = _crashed_frames(d)
    names = [n for n, _, _ in frames]
    symbols = " ".join(sym for _, _, sym in frames)
    if "obs-websocket" in names:
        return "websocket"
    if "CoreAudio" in names and ("obs_source_output_audio" in symbols or "copy_audio_data" in symbols):
        return "audio-device"
    if any("/PlugIns/" in p for _, p, _ in frames):
        return "source"
    return "unknown"


def crash_summary(path: Path, frames: int = 14) -> str:
    """The part of a crash report that says what broke: exception, crashed thread, plugin involved."""
    d = _read_ips(path)
    if d is None:
        return "\n".join(path.read_text(errors="replace").splitlines()[:60])   # OBS's own log, or unreadable
    exc, term = d.get("exception") or {}, d.get("termination") or {}
    out = [f"when: {d.get('captureTime', '?')} (OBS started {d.get('procLaunch', '?')})",
           "exception: " + " ".join(str(exc.get(k, "")) for k in ("type", "signal", "subtype")).strip()]
    if term.get("indicator"):
        out.append(f"termination: {term['indicator']}")
    for lib, msgs in (d.get("asi") or {}).items():
        out.append(f"{lib}: {'; '.join(map(str, msgs))}")
    idx, thread, shown = _crashed_frames(d, frames)
    out.append(f"crashed thread {idx}" + (f" ({thread['queue']})" if thread.get("queue") else "") + ":")
    out += [f"  {name[:30]:<30} {sym}" for name, _, sym in shown]
    plugin = next((n for n, p, _ in shown if "/PlugIns/" in p), None)
    apple = next((n for n, _, _ in shown if n in ("ScreenCaptureKit", "CoreMediaIO", "CoreAudio", "AVFCapture")), None)
    if plugin:
        out.append(f"OBS plugin involved: {plugin}")
    if apple:
        out.append(f"Apple framework involved: {apple}")
    if any(q in sym for q in _QUITTING for _, _, sym in shown):
        out.append("looks like OBS crashed while quitting (meetcap closes OBS after each meeting)")
    kind = crash_kind(path)
    if kind == "websocket":
        out.append("cause: obs-websocket crashed answering a request; the capture sources weren't at fault")
    elif kind == "audio-device":
        out.append("cause: OBS's microphone input crashed while handing over audio (an OBS bug when audio devices "
                   "reset or OBS quits); the screen source wasn't at fault")
    return "\n".join(out)


def display_uuids() -> list[str]:
    """UUIDs of the connected displays, main display first, in the form OBS's screen capture stores them.

    Read from CoreGraphics directly: asking OBS for this list (GetInputPropertiesListPropertyItems) crashes
    obs-websocket 5.x on macOS 26 / OBS 32.2 when the list has an entry without a value."""
    import ctypes
    import ctypes.util

    def lib(name: str):
        return ctypes.cdll.LoadLibrary(ctypes.util.find_library(name) or
                                       f"/System/Library/Frameworks/{name}.framework/{name}")
    try:
        cg, cf = lib("CoreGraphics"), lib("CoreFoundation")
        try:
            make_uuid = lib("ColorSync").CGDisplayCreateUUIDFromDisplayID
        except (OSError, AttributeError):
            make_uuid = cg.CGDisplayCreateUUIDFromDisplayID
    except (OSError, AttributeError):
        return []
    try:
        return _display_uuids(ctypes, cg, cf, make_uuid)
    except Exception as exc:  # noqa: BLE001 - fall back to OBS's default (the main display)
        log.warning("could not list displays: %s", exc)
        return []


def _display_uuids(ctypes, cg, cf, make_uuid) -> list[str]:
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    make_uuid.restype, make_uuid.argtypes = ctypes.c_void_p, [ctypes.c_uint32]
    cf.CFUUIDCreateString.restype, cf.CFUUIDCreateString.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ids, n = (ctypes.c_uint32 * 16)(), ctypes.c_uint32(0)
    if cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n)) != 0:
        return []
    main = cg.CGMainDisplayID()
    out = []
    for did in sorted(ids[:n.value], key=lambda x: x != main):
        uuid = make_uuid(did)
        if not uuid:
            continue
        text = cf.CFUUIDCreateString(None, uuid)
        buf = ctypes.create_string_buffer(64)
        if text and cf.CFStringGetCString(text, buf, 64, 0x08000100):   # kCFStringEncodingUTF8
            out.append(buf.value.decode())
        if text:
            cf.CFRelease(text)
        cf.CFRelease(uuid)
    return out


class Recorder(Protocol):
    def ensure_ready(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> Path | None: ...
    def is_recording(self) -> bool: ...


def obs_running() -> bool:
    return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def bmp_is_black(raw: bytes, threshold: int = 12) -> bool:
    """True when every pixel of a BMP is (nearly) black. Alpha bytes of 32-bit BMPs are ignored."""
    if raw[:2] != b"BM" or len(raw) < 54:
        raise ValueError("not a BMP")
    offset = int.from_bytes(raw[10:14], "little")
    bpp = int.from_bytes(raw[28:30], "little")
    px = raw[offset:]
    if bpp == 32:
        px = bytes(b for i, b in enumerate(px) if i % 4 != 3)
    return max(px, default=0) < threshold


def installed_obs_version(app_path: str) -> str | None:
    try:
        with open(Path(app_path) / "Contents" / "Info.plist", "rb") as fh:
            return str(plistlib.load(fh).get("CFBundleShortVersionString"))
    except (OSError, plistlib.InvalidFileException):
        return None


def clear_crash_sentinels() -> None:
    """After a crash or force-quit OBS shows a modal 'unclean shutdown / safe mode' dialog before the
    websocket starts, which would block a headless launch. Remove the markers it checks (31.x and 32.x)."""
    for marker in [*(OBS_SUPPORT / ".sentinel").glob("run_*"), OBS_SUPPORT / "safe_mode"]:
        try:
            marker.unlink()
            log.info("removed OBS crash marker %s", marker.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", marker, exc)


class _Steps(list):
    """A list that also reports each appended line (live progress for `meetcap setup-obs`)."""

    def __init__(self, on_step: Callable[[str], None] | None = None):
        super().__init__()
        self._on_step = on_step

    def append(self, item: str) -> None:  # noqa: D401
        super().append(item)
        if self._on_step:
            self._on_step(item)

    def extend(self, items) -> None:
        for item in items:
            self.append(item)


class ObsRecorder:
    def __init__(self, cfg: Config, password: Callable[[], str | None],
                 client_factory: Callable[..., ObsClient] = ObsClient):
        self.cfg = cfg
        self.obs = cfg.recording.obs
        self._password = password
        self._client_factory = client_factory
        self._client: ObsClient | None = None
        self._last_source: str | None = None  # source being added/configured, to blame if OBS dies

    # -- connection / lifecycle ---------------------------------------------------
    @property
    def client(self) -> ObsClient:
        if self._client is None:
            self._client = self._client_factory(self.obs.host, self.obs.port, self._password())
        return self._client

    def launch_args(self) -> list[str]:
        # --multi skips the "OBS did not shut down properly / safe mode" dialog that would block a
        # headless start after a crash (32.x removed --disable-shutdown-check). We guard single-instance
        # ourselves with pgrep before launching.
        return ["--minimize-to-tray", "--multi", "--disable-shutdown-check", "--disable-updater",
                "--profile", self.obs.profile, "--collection", self.obs.collection, "--scene", self.obs.scene]

    def launch(self, timeout: float | None = None) -> None:
        if not obs_running():
            clear_crash_sentinels()
            log.info("launching OBS")
            subprocess.run(["open", "-g", "-a", self.obs.app_path, "--args", *self.launch_args()], check=True)
        deadline = time.monotonic() + (timeout or self.obs.launch_timeout_seconds)
        while time.monotonic() < deadline:
            if self.client.ping():
                return
            time.sleep(1.0)
        raise RecorderError("OBS did not open its websocket in time. Run `meetcap doctor`.")

    def quit_obs(self, timeout: float = 30.0) -> None:
        """Graceful quit via Apple Events (one-time Automation prompt); SIGTERM if that is not allowed."""
        if not obs_running():
            return
        if self._client:
            self._client.close()
        subprocess.run(["osascript", "-e", 'tell application "OBS" to quit'], capture_output=True, timeout=15)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and obs_running():
            if time.monotonic() > deadline - timeout / 2:
                subprocess.run(["pkill", "-TERM", "-x", "OBS"], capture_output=True)
            time.sleep(0.5)
        if obs_running():
            raise RecorderError("OBS did not quit. Close it manually and retry.")

    def ensure_ready(self) -> None:
        """Make OBS record the MeetCap way. Repairs what it can live (no OBS restart): profile, scene
        collection, scene, the screen / system-audio / mic sources and their tracks."""
        if not self.client.ping():
            self.launch()
        if self.is_recording():
            return
        try:
            self.ensure_profile()
        except ObsRequestError as exc:
            log.warning("could not switch OBS to the MeetCap profile (%s)", exc)
        notes: list[str] = []
        try:
            self._safely(self.ensure_scene, notes, installed_obs_version(self.obs.app_path))
        except (ObsRequestError, RecorderError) as exc:
            log.warning("OBS scene not fully ready (%s); recording anyway", exc)
        for n in notes:
            if n.startswith(("WARNING: ", "PROBLEM: ")):
                log.warning("OBS: %s", n)

    def ensure_profile(self, steps: list[str] | None = None) -> None:
        c = self.client
        profiles = c.call("GetProfileList")
        if self.obs.profile not in profiles.get("profiles", []):
            c.call("CreateProfile", {"profileName": self.obs.profile})  # also switches to it
            self.write_profile_params()
            try:
                self._apply_video_settings()
            except ObsRequestError as exc:
                log.warning("video settings not applied: %s", exc)
            if steps is not None:
                steps.append(f"created profile {self.obs.profile}")
        elif profiles.get("currentProfileName") != self.obs.profile:
            c.call("SetCurrentProfile", {"profileName": self.obs.profile})

    def write_profile_params(self) -> str:
        fmt = "hybrid_mp4"
        out_dir = str(expand(self.cfg.recording.output_dir))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        for cat, name, value in (
            ("Output", "Mode", "Simple"),
            ("Output", "FilenameFormatting", FILENAME_FORMAT),
            ("SimpleOutput", "FilePath", out_dir),
            ("SimpleOutput", "RecFormat2", fmt),
            ("SimpleOutput", "RecQuality", "Small"),
            ("SimpleOutput", "RecEncoder", "apple_h264"),
            ("SimpleOutput", "RecAudioEncoder", "aac"),
            ("SimpleOutput", "RecTracks", "7"),  # bitmask: tracks 1, 2 and 3
        ):
            self.client.call("SetProfileParameter", {"parameterCategory": cat, "parameterName": name,
                                                     "parameterValue": value})
        return fmt

    def mic_input_name(self) -> str:
        try:
            return self.client.call("GetSpecialInputs").get("mic1") or MIC_INPUT
        except ObsRequestError:
            return MIC_INPUT

    # -- sources that crash OBS on this Mac ------------------------------------------
    @property
    def quarantined(self) -> set[str]:
        return load_quarantine(installed_obs_version(self.obs.app_path))

    @property
    def video_available(self) -> bool:
        return SCREEN_INPUT not in self.quarantined

    def ensure_scene(self, steps: list[str] | None = None, strict: bool = False, settle: float = 0.0) -> None:
        """Idempotent: scene collection, scene, then mic -> meeting audio -> screen (audio first), display
        picked, tracks routed. With `settle`, waits after adding each source and raises SourceCrashed if
        OBS died, so setup can leave that source out. Sources in quarantine are never added."""
        c = self.client
        note = steps.append if steps is not None else (lambda _m: None)
        skip = self.quarantined
        cols = c.call("GetSceneCollectionList")
        if self.obs.collection not in cols.get("sceneCollections", []):
            c.call("CreateSceneCollection", {"sceneCollectionName": self.obs.collection})
            note(f"created scene collection {self.obs.collection}")
            time.sleep(2)
        elif cols.get("currentSceneCollectionName") != self.obs.collection:
            c.call("SetCurrentSceneCollection", {"sceneCollectionName": self.obs.collection})
            time.sleep(2)  # collection switch finishes asynchronously

        scenes = [x["sceneName"] for x in c.call("GetSceneList").get("scenes", [])]
        if self.obs.scene not in scenes:
            c.call("CreateScene", {"sceneName": self.obs.scene})
            note(f"created scene {self.obs.scene}")
        c.call("SetCurrentProgramScene", {"sceneName": self.obs.scene})

        inputs = {i["inputName"] for i in c.call("GetInputList").get("inputs", [])}

        def alive_after(name: str, request: str, data: dict) -> None:
            self._last_source = name
            try:
                c.call(request, data)
            except ObsConnectionError as exc:
                raise SourceCrashed(name) from exc
            if settle:
                time.sleep(settle)
                if not c.ping():
                    raise SourceCrashed(name)

        def add(name: str, kind: str, settings: dict, label: str) -> bool:
            if name in inputs or name in skip:
                return False
            alive_after(name, "CreateInput", {"sceneName": self.obs.scene, "inputName": name, "inputKind": kind,
                                              "inputSettings": settings, "sceneItemEnabled": True})
            inputs.add(name)
            note(label)
            return True

        special = c.call("GetSpecialInputs")
        if special.get("desktop1"):
            c.call("SetInputMute", {"inputName": special["desktop1"], "inputMuted": True})
            note(f"muted global desktop audio '{special['desktop1']}' to avoid double audio")
        if special.get("mic1"):
            note(f"using OBS global mic '{special['mic1']}'")
        else:
            add(MIC_INPUT, "coreaudio_input_capture", {"device_id": "default"},
                "added microphone (follows the system default input)")
        add(SYSTEM_AUDIO_INPUT, "sck_audio_capture", {"type": 0}, "added meeting audio (macOS Audio Capture)")
        add(SCREEN_INPUT, "screen_capture", {"type": 0, "show_cursor": True}, "added macOS Screen Capture")

        if SCREEN_INPUT in inputs:
            displays = display_uuids()
            if displays:
                try:
                    current = c.call("GetInputSettings", {"inputName": SCREEN_INPUT}).get("inputSettings", {})
                except ObsConnectionError as exc:
                    raise SourceCrashed(SCREEN_INPUT) from exc
                if current.get("display_uuid") not in displays:
                    pick = displays[min(self.cfg.recording.display_index, len(displays) - 1)]
                    alive_after(SCREEN_INPUT, "SetInputSettings", {
                        "inputName": SCREEN_INPUT, "overlay": True, "inputSettings": {"display_uuid": pick}})
                    note("capturing the main display" if pick == displays[0] else f"capturing display {pick}")
            # no list (not macOS): OBS's own default is the main display

        # Meeting audio normally comes from its own source so video can be switched off independently. If that
        # source crashes OBS here, use the screen source's built-in audio instead.
        screen_audio = SYSTEM_AUDIO_INPUT not in inputs and SCREEN_INPUT in inputs
        if SCREEN_INPUT in inputs:
            try:
                c.call("SetInputMute", {"inputName": SCREEN_INPUT, "inputMuted": not screen_audio})
            except ObsRequestError:
                pass

        # Separate tracks so transcripts can say "You" vs "Others": 1 = mix (for playback), 2 = mic, 3 = meeting.
        mic_name = special.get("mic1") or MIC_INPUT
        routes = [(mic_name, (True, True, False)), (SYSTEM_AUDIO_INPUT, (True, False, True)),
                  (SCREEN_INPUT, (True, False, screen_audio))]
        for name, tracks in routes:
            if name not in inputs and name != special.get("mic1"):
                continue
            try:
                c.call("SetInputAudioTracks", {"inputName": name, "inputAudioTracks": {
                    str(i): (tracks[i - 1] if i <= 3 else False) for i in range(1, 7)}})
            except ObsRequestError as exc:
                log.warning("could not set audio tracks on %s: %s", name, exc)
        note("audio tracks: 1 = everything, 2 = your mic, 3 = meeting audio")
        if settle and self._last_source:  # delayed crashes: give the last source a moment more
            time.sleep(settle)
            if not c.ping():
                raise SourceCrashed(self._last_source)

    # -- capture health -----------------------------------------------------------
    def screen_is_black(self) -> bool | None:
        """True if the screen source only produces black (Screen Recording permission missing)."""
        import base64

        try:
            r = self.client.call("GetSourceScreenshot", {"sourceName": SCREEN_INPUT, "imageFormat": "bmp",
                                                         "imageWidth": 64})
            data = r.get("imageData", "")
            return bmp_is_black(base64.b64decode(data.split(",", 1)[-1]))
        except (ObsRequestError, ObsConnectionError, ValueError) as exc:
            log.debug("screenshot check unavailable: %s", exc)
            return None

    def levels(self, seconds: float = 3.0) -> dict[str, float]:
        """Peak linear level per audio input over `seconds` (0.0 = OBS receives digital silence)."""
        from .obs_client import EVENT_INPUT_VOLUME_METERS

        listener = self._client_factory(self.obs.host, self.obs.port, self._password(),
                                        event_subscriptions=EVENT_INPUT_VOLUME_METERS)
        try:
            events = listener.read_events(seconds, "InputVolumeMeters")
        finally:
            listener.close()
        peak: dict[str, float] = {}
        for ev in events:
            for inp in ev.get("inputs", []):
                vals = [abs(float(v)) for ch in inp.get("inputLevelsMul", []) for v in ch]
                name = inp.get("inputName", "?")
                peak[name] = max(peak.get(name, 0.0), max(vals, default=0.0))
        return peak

    def check_capture(self, seconds: float = 3.0) -> list[str]:
        """Plain-language problems with what OBS is capturing right now. [] = looks fine."""
        problems: list[str] = []
        if self.screen_is_black():
            problems.append("OBS only sees a black screen. Allow OBS in System Settings > Privacy & Security > "
                            "Screen & System Audio Recording, then quit OBS (Cmd+Q).")
        try:
            lv = self.levels(seconds)
        except (ObsConnectionError, ObsRequestError) as exc:
            log.debug("level check unavailable: %s", exc)
            return problems
        mic = lv.get(self.mic_input_name())
        if mic is None:
            problems.append("OBS has no microphone source. Run: meetcap setup-obs")
        elif mic <= 0.0:
            problems.append("OBS hears nothing from your microphone. Allow OBS in System Settings > Privacy & "
                            "Security > Microphone, then quit OBS (Cmd+Q).")
        return problems

    # -- recording ----------------------------------------------------------------
    def is_recording(self) -> bool:
        try:
            return bool(self.client.call("GetRecordStatus").get("outputActive"))
        except (ObsConnectionError, ObsRequestError):
            return False

    def set_video_enabled(self, enabled: bool) -> None:
        try:
            item = self.client.call("GetSceneItemId", {"sceneName": self.obs.scene, "sourceName": SCREEN_INPUT})
            self.client.call("SetSceneItemEnabled", {"sceneName": self.obs.scene,
                                                     "sceneItemId": item["sceneItemId"],
                                                     "sceneItemEnabled": enabled})
        except ObsRequestError as exc:
            log.warning("could not toggle screen source: %s", exc)

    def start(self) -> None:
        self.ensure_ready()
        self.set_video_enabled(self.cfg.recording.video)
        try:
            self.client.call("StartRecord")
        except ObsRequestError as exc:
            if exc.code != OUTPUT_RUNNING:
                raise RecorderError(f"OBS refused to start recording: {exc}") from exc
            log.info("OBS was already recording; adopting that recording")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.is_recording():
                return
            time.sleep(0.5)
        raise RecorderError("OBS did not report an active recording. Check permissions in OBS.")

    def stop(self) -> Path | None:
        try:
            resp = self.client.call("StopRecord")
        except ObsRequestError as exc:
            if exc.code == OUTPUT_NOT_RUNNING:
                log.warning("stop requested but OBS was not recording")
                return None
            raise RecorderError(str(exc)) from exc
        except ObsConnectionError as exc:
            log.error("OBS unreachable at stop (%s); the file will be picked up by the orphan scan", exc)
            return None
        path = resp.get("outputPath")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and self.is_recording():  # StopRecord does not block
            time.sleep(0.5)
        return Path(path) if path else None

    # -- one-time setup -----------------------------------------------------------
    def write_websocket_config(self, password: str) -> None:
        """Enable obs-websocket with auth. OBS must be closed: it rewrites this file on load and exit."""
        self.quit_obs()
        WEBSOCKET_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if WEBSOCKET_CONFIG.exists():
            try:
                data = json.loads(WEBSOCKET_CONFIG.read_text())
            except ValueError:
                data = {}
        data.update(server_enabled=True, server_port=self.obs.port, auth_required=True,
                    server_password=password, first_load=False, alerts_enabled=False)
        WEBSOCKET_CONFIG.write_text(json.dumps(data, indent=2))

    def setup(self, set_password: Callable[[str], None],
              on_step: Callable[[str], None] | None = None) -> list[str]:
        """Idempotently build the MeetCap profile, scene collection, scene and sources. Returns a log;
        `on_step` sees each line as it happens."""
        steps: list[str] = _Steps(on_step)
        installed = installed_obs_version(self.obs.app_path)
        if installed is None:
            raise RecorderError(f"OBS not found at {self.obs.app_path}. brew install --cask obs")
        if _version_tuple(installed) < OBS_MIN_VERSION:
            raise RecorderError(f"OBS {installed} is too old; 30.2+ is required. brew upgrade --cask obs")
        password = self._password() or secrets.token_urlsafe(24)
        set_password(password)
        self.write_websocket_config(password)  # OBS stays closed from here until launch()
        steps.append(f"obs-websocket enabled on port {self.obs.port} (password stored in Keychain)")
        save_quarantine(set(), installed)       # every setup gives every source a fresh try
        if remove_sources_from_collection(self.obs.collection, [MIC_INPUT, SYSTEM_AUDIO_INPUT, SCREEN_INPUT]):
            steps.append("cleared meetcap's old sources in OBS; adding them back one at a time")
        self._client = None
        self.launch(timeout=300)  # first run shows macOS permission dialogs before the websocket starts

        # OBS can close mid-setup: granting Screen Recording makes macOS "Quit & Reopen" it, and on some
        # macOS/OBS combinations a capture source crashes it outright. Every step is idempotent: sources are
        # added one at a time, and one that takes OBS down is left out (audio first) instead of looping.
        fmt = self._configure_safely(steps, installed)

        self.quit_obs()      # muxer/encoder settings are read when OBS builds its outputs
        self._client = None
        self.launch(timeout=180)

        def verify() -> None:
            got = self.client.call("GetProfileParameter", {"parameterCategory": "SimpleOutput",
                                                           "parameterName": "RecFormat2"}).get("parameterValue")
            if got != fmt:
                raise RecorderError(f"OBS profile did not keep the recording format (got {got!r}). "
                                    "Re-run: meetcap setup-obs")
            try:
                self.ensure_scene(settle=SETTLE_SECONDS)
            except SourceCrashed:
                raise
            except (ObsRequestError, RecorderError) as exc:
                log.warning("scene check after restart: %s", exc)

        def verify_and_settle() -> None:
            verify()
            time.sleep(SETTLE_SECONDS)       # sources are live again after the restart; make sure OBS stays up
            if not self.client.ping():
                raise ObsConnectionError("OBS closed after the restart")

        self._safely(verify_and_settle, steps, installed)
        steps.append("restarted OBS with the MeetCap profile and verified it")
        problems = self.check_capture(4)
        steps.extend(f"PROBLEM: {p}" for p in problems)
        if not problems:
            steps.append("OBS sees your screen and hears your microphone" if self.video_available
                         else "OBS hears your microphone and will record the meeting audio (video is off, see above)")
        if not self.cfg.recording.keep_obs_running:
            self.quit_obs()
            steps.append("closed OBS; meetcap opens it when a meeting starts and closes it afterwards")
        return steps

    def _configure(self, steps: list[str]) -> str:
        ver = self.client.call("GetVersion")
        line = f"OBS {ver.get('obsVersion', '?')}, obs-websocket {ver.get('obsWebSocketVersion')}"
        if line not in steps:
            steps.append(line)
        self.ensure_profile(steps)
        self.ensure_scene(steps, strict=True, settle=SETTLE_SECONDS)
        fmt = self.write_profile_params()
        steps.append(f"recording to {expand(self.cfg.recording.output_dir)} as {fmt} (crash-safe), Apple H.264")
        steps.append(self._apply_video_settings())
        return fmt

    def _configure_safely(self, steps: list[str], obs_version: str | None) -> str:
        return self._safely(lambda: self._configure(steps), steps, obs_version)

    def _safely(self, fn: Callable[[], object], steps: list[str], obs_version: str | None):
        """Run `fn`, surviving OBS going away. If it died after meetcap touched a source, that source is the
        cause: leave it out (quarantine), strip it from the saved scene, reopen OBS and run again. If it died
        before any source was touched, it's macOS's 'Quit & Reopen' after a permission change: just reopen."""
        bad = set(self.quarantined)
        reopens = innocent = 0
        for _ in range(8):
            self._last_source = None
            started = time.time()
            try:
                return fn()
            except SourceCrashed as exc:
                culprit = exc.name
            except ObsConnectionError as exc:
                if self._last_source:
                    culprit = self._last_source
                else:
                    reopens += 1
                    if reopens > 3:
                        raise RecorderError(
                            "OBS keeps closing during setup. Open OBS yourself once (Applications > OBS), click "
                            "Allow on every prompt and make sure it stays open, quit it with Cmd+Q, then run "
                            "`meetcap setup-obs` again.") from exc
                    log.warning("OBS went away before any source was touched (%s); reopening", exc)
                    steps.append("OBS closed during setup (normal right after allowing a permission); reopened it "
                                 "and continued")
                    self._client = None
                    time.sleep(3)
                    self.launch(timeout=180)
                    continue
            self._wait_until_gone()
            report = self._crash_report_since(started)
            kind = crash_kind(report) if report else "unknown"
            if kind in ("websocket", "audio-device"):      # OBS's own bug, not the source meetcap just touched
                innocent += 1
                what = "websocket" if kind == "websocket" else "microphone input"
                if innocent > 3:
                    raise RecorderError(f"OBS keeps crashing in its own {what} code. Run `meetcap obs-log` and "
                                        "send the output.")
                steps.append(f"OBS crashed in its {what} code, not in {culprit}; reopened it and tried again")
                self._client = None
                self.launch(timeout=180)
                continue
            bad.add(culprit)
            save_quarantine(bad, obs_version)
            level, advice = crash_advice(culprit, bad)
            where = latest_obs_crash() or latest_obs_log()
            steps.append(f"{level}: {advice}" + (f" (OBS's report: {where})" if where else ""))
            remove_sources_from_collection(self.obs.collection, [culprit])
            self._client = None
            self.launch(timeout=180)
        raise RecorderError("OBS crashes with every source meetcap adds. Run `meetcap obs-log` and send the output.")

    @staticmethod
    def _crash_report_since(since: float, wait: float | None = None) -> Path | None:
        """The report macOS writes for a crash that happened after `since` (it can take ~10 s to appear)."""
        if not DIAG_DIR.exists():
            return None
        deadline = time.monotonic() + (CRASH_REPORT_WAIT if wait is None else wait)
        while True:
            found = [p for p in obs_crashes(days=1, limit=3) if p.stat().st_mtime >= since - 1]
            if found or time.monotonic() >= deadline:
                return found[0] if found else None
            time.sleep(1)

    def close_after_recording(self, timeout: float = 20.0) -> None:
        """End OBS once a recording is written, without its normal quit.

        OBS 32.2 on macOS crashes in its microphone input during a normal quit (every time on the M3 /
        macOS 26.6 setup). By now the file is complete and OBS has nothing left to save, so meetcap ends the
        process directly and removes the 'didn't shut down cleanly' marker, so the next launch is silent."""
        if not obs_running():
            return
        try:
            if self.is_recording():
                log.warning("OBS is recording again; leaving it open")
                return
        except (ObsConnectionError, ObsRequestError, RecorderError):
            pass
        if self._client:
            self._client.close()
            self._client = None
        subprocess.run(["pkill", "-KILL", "-x", "OBS"], capture_output=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and obs_running():
            time.sleep(0.3)
        clear_crash_sentinels()
        log.info("closed OBS after the recording")

    def _wait_until_gone(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and obs_running():
            time.sleep(0.5)
        if obs_running():  # hung in its crash handler
            subprocess.run(["pkill", "-9", "-x", "OBS"], capture_output=True)
            time.sleep(1)

    def _apply_video_settings(self) -> str:
        c = self.client
        vs = c.call("GetVideoSettings")
        base_w, base_h = int(vs.get("baseWidth", 1920)), int(vs.get("baseHeight", 1080))
        try:
            item = c.call("GetSceneItemId", {"sceneName": self.obs.scene, "sourceName": SCREEN_INPUT})
            for _ in range(10):  # source size is 0 until the first frame arrives
                tr = c.call("GetSceneItemTransform", {"sceneName": self.obs.scene,
                                                      "sceneItemId": item["sceneItemId"]})["sceneItemTransform"]
                if tr.get("sourceWidth"):
                    base_w, base_h = int(tr["sourceWidth"]), int(tr["sourceHeight"])
                    break
                time.sleep(0.5)
        except ObsRequestError as exc:
            log.warning("could not read display size: %s", exc)
            item = None
        if base_w > 4096:  # obs-websocket rejects canvas sizes above 4096
            base_h, base_w = int(base_h * 4096 / base_w) // 2 * 2, 4096
        out_w = min(self.cfg.recording.max_width, base_w)
        out_h = int(round(base_h * out_w / base_w / 2)) * 2
        c.call("SetVideoSettings", {"fpsNumerator": int(self.cfg.recording.fps), "fpsDenominator": 1,
                                    "baseWidth": base_w, "baseHeight": base_h,
                                    "outputWidth": out_w, "outputHeight": out_h})
        if item:
            c.call("SetSceneItemTransform", {"sceneName": self.obs.scene, "sceneItemId": item["sceneItemId"],
                                             "sceneItemTransform": {"positionX": 0, "positionY": 0,
                                                                    "boundsType": "OBS_BOUNDS_SCALE_INNER",
                                                                    "boundsAlignment": 0,
                                                                    "boundsWidth": base_w,
                                                                    "boundsHeight": base_h}})
        return f"canvas {base_w}x{base_h} -> output {out_w}x{out_h} @ {self.cfg.recording.fps} fps"
