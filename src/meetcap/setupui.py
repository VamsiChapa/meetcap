"""The setup screen: one local page for first-run setup and every later change. `meetcap setup` opens it.

It runs on 127.0.0.1 on a free port with a one-time token. The token travels in the URL fragment (never
sent to a server or kept in history by a web request) and comes back in an `X-Meetcap-Token` header, so
only the tab meetcap opened can change settings. API keys go straight to the macOS Keychain and are never
sent back to the page: it only learns whether each one is set.
"""
from __future__ import annotations

import json
import logging
import platform
import secrets as pysecrets
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import health, providers
from .config import (CONFIG_PATH, STATE_DIR, STORAGE_BACKENDS, SUMMARY_PROVIDERS, TRANSCRIPTION_ENGINES, Config,
                     load_config, problems)
from .pet import SPECIES, available_species, sprite_resource
from .vault import KNOWN_SECRETS, delete_secret, get_secret, set_secret

log = logging.getLogger(__name__)

# What the page may change, and the type each value must have.
EDITABLE: dict[str, type] = {
    "storage.backend": str, "storage.local_path": str,
    "azure.storage.auth": str, "azure.storage.account_url": str, "azure.storage.container": str,
    "azure.storage.prefix": str,
    "transcription.engine": str, "transcription.model": str, "transcription.base_url": str,
    "azure.speech.endpoint": str, "azure.speech.locales": list, "azure.speech.phrase_list": list,
    "azure.openai.endpoint": str, "azure.openai.deployment": str, "azure.openai.transcribe_deployment": str,
    "summary.provider": str, "summary.model": str, "summary.base_url": str, "summary.screenshots": bool,
    "detection.enabled": bool, "recording.video": bool, "processing.user_name": str,
    "ui.pet": bool, "ui.pet_species": str, "ui.pet_size": int, "ui.open_notes_after_meeting": bool,
}
CHOICES: dict[str, tuple[str, ...]] = {
    "storage.backend": STORAGE_BACKENDS, "transcription.engine": TRANSCRIPTION_ENGINES,
    "summary.provider": SUMMARY_PROVIDERS, "azure.storage.auth": ("key", "sas", "aad"), "ui.pet_species": tuple(SPECIES),
}
PAGE_SECRETS = sorted(set(KNOWN_SECRETS) - {"obs-password"})
MAX_BODY = 64 * 1024


def _get(cfg: Config, dotted: str) -> Any:
    obj: Any = cfg
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def environment(cfg: Config, get: Callable[[str], str | None] = get_secret) -> dict[str, Any]:
    from . import local_whisper
    whisper_ok, whisper_why = local_whisper.supported()
    return {
        "mac": sys.platform == "darwin", "apple_silicon": platform.machine() == "arm64",
        "macos": platform.mac_ver()[0], "whisper_ok": whisper_ok, "whisper_why": whisper_why,
        "whisper_installed": whisper_ok and local_whisper.installed(),
        "obs_installed": Path(cfg.recording.obs.app_path).exists(), "obs_set_up": bool(get("obs-password")),
        "ffmpeg": bool(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").exists()),
        "config_path": str(CONFIG_PATH), "reader": str(cfg.reader_path),
    }


class SetupSession:
    """State behind the page. The Keychain and the OBS setup are injectable, so tests don't touch either."""

    def __init__(self, config_path: Path = CONFIG_PATH, get: Callable[[str], str | None] = get_secret,
                 put: Callable[[str, str], None] = set_secret, remove: Callable[[str], None] = delete_secret,
                 obs_setup: Callable[..., Any] | None = None, restart: Callable[[], str] | None = None):
        self.token = pysecrets.token_urlsafe(24)
        self.config_path, self._get, self._put, self._remove = config_path, get, put, remove
        self._obs_setup, self._restart = obs_setup, restart
        self.obs = {"running": False, "steps": [], "error": "", "done": False}
        self.finished = threading.Event()
        self.last_seen = time.monotonic()

    def cfg(self) -> Config:
        return load_config(self.config_path)

    # -- reading ------------------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        cfg = self.cfg()
        presets = {k: {"label": p.label, "stt_model": p.stt_model, "chat_model": p.chat_model, "signup": p.signup,
                       "free": p.free, "secret": p.secret, "vision": p.vision} for k, p in providers.PRESETS.items()}
        return {"config": {k: _get(cfg, k) for k in EDITABLE},
                "secrets": {name: bool(self._get(name)) for name in PAGE_SECRETS},
                "needed": health.secrets_needed(cfg), "problems": problems(cfg), "describe": providers.describe(cfg),
                "env": environment(cfg, self._get), "presets": presets,
                "species": [{"id": s, "label": SPECIES[s]} for s in available_species()], "obs": self.obs}

    # -- writing ------------------------------------------------------------------------------------------------
    def save(self, values: dict[str, Any], secrets_in: dict[str, Any]) -> dict[str, Any]:
        from .azure_setup import update_config

        updates: dict[str, Any] = {}
        for key, value in (values or {}).items():
            want = EDITABLE.get(key)
            if want is None:
                raise ValueError(f"'{key}' can't be changed here")
            if (want is int and isinstance(value, bool)) or not isinstance(value, want):
                raise ValueError(f"'{key}' must be {want.__name__}")
            if want is list:
                value = [str(v).strip() for v in value if str(v).strip()]
            if want is str:
                value = value.strip()
            updates[key] = value
        for key, allowed in CHOICES.items():
            if key in updates and updates[key] not in allowed:
                raise ValueError(f"'{key}' must be one of: {', '.join(allowed)}")
        for name, value in (secrets_in or {}).items():
            if name not in PAGE_SECRETS:
                raise ValueError(f"unknown key '{name}'")
            if value is None or value == "":
                continue                          # empty field = keep what's stored
            if value is False:
                self._remove(name)
            elif isinstance(value, str):
                self._put(name, value.strip())
        if updates:
            update_config(self.config_path, updates)
        return self.state()

    def test(self, what: str) -> dict[str, Any]:
        cfg = self.cfg()
        fn = {"storage": health.storage, "transcription": health.transcription, "summary": health.summary}.get(what)
        if fn is None:
            raise ValueError("test one of: storage, transcription, summary")
        ok, message = fn(cfg, self._get)
        return {"ok": ok, "message": message}

    def start_obs(self) -> dict[str, Any]:
        if self.obs["running"]:
            return self.obs
        self.obs = {"running": True, "steps": [], "error": "", "done": False}

        def run() -> None:
            try:
                if self._obs_setup is not None:
                    self._obs_setup(self.cfg(), on_step=self.obs["steps"].append)
                else:
                    from .agent import setup_obs
                    setup_obs(self.cfg(), on_step=self.obs["steps"].append)
                self.obs["done"] = True
            except Exception as exc:  # noqa: BLE001 - shown on the page
                self.obs["error"] = str(exc)[:500]
            finally:
                self.obs["running"] = False
        threading.Thread(target=run, name="meetcap-setup-obs", daemon=True).start()
        return self.obs

    def finish(self) -> dict[str, Any]:
        cfg = self.cfg()
        reader = ""
        if cfg.storage.backend == "local":
            try:
                from .reader import publish_static
                reader = str(publish_static(cfg.local_root, species=cfg.ui.pet_species))
            except Exception as exc:  # noqa: BLE001
                log.warning("reader: %s", exc)
        elif cfg.reader_path.exists():
            reader = str(cfg.reader_path)          # the Azure page made earlier by `meetcap reader`
        restarted = (self._restart or restart_agent)()
        if reader and self._restart is None:
            subprocess.Popen(["open", reader])
        self.finished.set()
        return {"restarted": restarted, "reader": reader}

    def choose_folder(self) -> dict[str, Any]:
        """macOS folder picker (runs on the Mac, not in the page)."""
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt '
                            '"Where should meetcap keep your meetings?")'], capture_output=True, text=True)
        return {"path": r.stdout.strip().rstrip("/")} if r.returncode == 0 else {"path": ""}


def restart_agent() -> str:
    from .cli import LAUNCH_LABEL
    import os
    target = f"gui/{os.getuid()}/{LAUNCH_LABEL}"
    if subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0:
        return "meetcap isn't installed as a login item; start it with: meetcap menubar"
    r = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
    return "restarted meetcap with the new settings" if r.returncode == 0 else f"restart failed: {r.stderr.strip()}"


def make_handler(session: SetupSession, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
           "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'")

    class Handler(BaseHTTPRequestHandler):
        server_version = "meetcap-setup"

        def log_message(self, fmt, *args):
            log.debug("setup %s", fmt % args)

        def _send(self, code, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            for k, v in {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code=HTTPStatus.OK) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

        def _allowed(self, api: bool) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return False
            if api and not pysecrets.compare_digest(self.headers.get("X-Meetcap-Token", ""), session.token):
                self._json({"error": "This page is out of date. Run meetcap setup again."}, HTTPStatus.FORBIDDEN)
                return False
            session.last_seen = time.monotonic()
            return True

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(api=path.startswith("/api/")):
                return
            try:
                self._get_route([p for p in path.split("/") if p])
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("setup request failed")
                self._json({"error": str(exc)[:300]}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _get_route(self, parts: list[str]) -> None:
            if parts in ([], ["setup"]):
                page = resources.files("meetcap").joinpath("web/setup.html").read_bytes()
                return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            if len(parts) == 3 and parts[0] == "pet" and parts[1] in SPECIES and parts[2] in ("sit_r_0.png", "walk_r_0.png"):
                data = resources.files("meetcap").joinpath(sprite_resource(parts[1], parts[2][:-4])).read_bytes()
                return self._send(HTTPStatus.OK, data, "image/png", {"Cache-Control": "max-age=3600"})
            if parts == ["favicon.png"]:
                return self._send(HTTPStatus.OK, resources.files("meetcap").joinpath("icons/idle_0.png").read_bytes(),
                                  "image/png")
            if parts == ["api", "state"]:
                return self._json(session.state())
            if parts == ["api", "obs"]:
                return self._json(session.obs)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(api=True):
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json({"error": "too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/save":
                    return self._json(session.save(body.get("config") or {}, body.get("secrets") or {}))
                if path == "/api/test":
                    return self._json(session.test(str(body.get("what", ""))))
                if path == "/api/obs":
                    return self._json(session.start_obs())
                if path == "/api/finish":
                    return self._json(session.finish())
                if path == "/api/choose-folder":
                    return self._json(session.choose_folder())
            except (ValueError, TypeError) as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                log.exception("setup request failed")
                return self._json({"error": str(exc)[:300]}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(session: SetupSession, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    real = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(session, real)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="meetcap-setup-web", daemon=True).start()
    return httpd, f"http://127.0.0.1:{real}/setup#t={session.token}"


def run(open_browser: bool = True, idle_minutes: float = 45) -> None:
    """`meetcap setup`: open the page and wait until Finish (or the tab has been idle a long time)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    session = SetupSession()
    httpd, url = serve(session)
    print(f"Setup is open in your browser. If it didn't open, go to:\n  {url}\n(Ctrl+C to stop)")
    if open_browser:
        subprocess.run(["open", url], check=False)
    try:
        while not session.finished.wait(2):
            if time.monotonic() - session.last_seen > idle_minutes * 60:
                print("Setup closed after being idle. Run meetcap setup to open it again.")
                break
        time.sleep(1)       # let the page receive the Finish reply
    except KeyboardInterrupt:
        pass
    httpd.shutdown()
