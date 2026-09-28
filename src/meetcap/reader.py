"""The notes reader as a file you open in the browser. Works whether or not meetcap is running.

Two kinds, both built from the same `web/index.html`:
- Local folder (storage.backend = local): `<folder>/Meeting Notes.html` plus small script files next to the
  meetings (`meetcap-index.js`, and `reader.js` in each meeting folder). Browsers won't let a page opened
  from a file read JSON files beside it, but they do load scripts, so the data ships as scripts.
- Azure: one file with a read-only link baked in and a Content-Security-Policy that only allows Azure Blob.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import STATE_DIR

log = logging.getLogger(__name__)
READER_FILE = STATE_DIR / "Meeting Notes.html"
BLOB_HOST = re.compile(r"^[a-z0-9]{3,24}\.blob\.core\.(windows\.net|chinacloudapi\.cn|usgovcloudapi\.net)$")
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data: https://*.blob.core.windows.net; media-src https://*.blob.core.windows.net; "
       "connect-src https://*.blob.core.windows.net; base-uri 'none'; form-action 'none'")


class ReaderLinkError(ValueError):
    pass


def check_link(link: str) -> dict:
    """Validate a reader link and describe it: account host, container, prefix, expiry."""
    u = urlparse(link.strip())
    if u.scheme != "https" or not BLOB_HOST.match(u.hostname or ""):
        raise ReaderLinkError("a reader link starts with https://<account>.blob.core.windows.net/")
    q = parse_qs(u.query)
    if "sig" not in q:
        raise ReaderLinkError("the link has no signature (sig=…); copy the whole line")
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        raise ReaderLinkError("the link needs the container name after the account address")
    return {"host": u.hostname, "container": parts[0], "prefix": "/".join(parts[1:]),
            "expires": q.get("se", [None])[0]}


PETS = ("sit_r_0", "sit_r_1", "sleep_r_0", "sleep_r_1", "listen_r_0", "write_r_0", "alert_r_0")
STATIC_HTML = "Meeting Notes.html"
STATIC_INDEX = "meetcap-index.js"
MEETING_JS = "reader.js"


def _png(name: str) -> str:
    data = resources.files("meetcap").joinpath(name).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _pet_uris(species: str = "puppy") -> dict[str, str]:
    from .pet import sprite_resource
    return {n: _png(sprite_resource(species, n)) for n in PETS}


def _script_json(data) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _page(config: dict, head: str) -> str:
    page = resources.files("meetcap").joinpath("web/index.html").read_text()
    head += f'<link rel="icon" href="{_png("icons/idle_0.png")}">\n'
    page = page.replace('<link rel="icon" href="/favicon.png">\n', head, 1)
    return page.replace('<script>\n"use strict";', f"<script>window.MEETCAP_READER = {_script_json(config)};</script>\n"
                        '<script>\n"use strict";', 1)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    tmp.replace(path)


# -- local folder -----------------------------------------------------------------------------------------
def meeting_record(root: Path, folder: Path) -> dict:
    from .library import _phrases

    meta = json.loads((folder / "meta.json").read_text())
    notes = json.loads((folder / "notes.json").read_text()) if (folder / "notes.json").exists() else {}
    try:
        phrases = _phrases(json.loads((folder / "transcript.json").read_text()))
    except (OSError, ValueError):
        phrases = []
    frames = sorted(f.name for f in (folder / "frames").glob("*.jpg")) if (folder / "frames").is_dir() else []
    audio = next((n for n in ("audio.m4a", "audio.ogg") if (folder / n).exists()), None)
    video = next((f.name for f in sorted(folder.glob("recording.*")) if f.suffix.lower() in (".mp4", ".mov", ".mkv")), None)
    rel = folder.relative_to(root).as_posix()
    return {"id": str(meta.get("id") or rel), "folder": rel, "meta": meta, "notes": notes, "transcript": phrases,
            "frames": frames, "audio": audio, "video": video}


def publish_static(root: Path, folder: Path | None = None, audio: str | None = None, species: str | None = None) -> Path:
    """Write (or refresh) the reader for a local meetings folder. `folder` = the meeting just finished;
    without it, every meeting's reader.js is rewritten (after an upgrade, or `meetcap reader`)."""
    from .library import MeetingLibrary, _search_text

    root = Path(root)
    if folder is not None and (folder / "audio.ogg").exists() and not (folder / "audio.m4a").exists():
        try:  # Safari doesn't play Opus; AAC plays everywhere (~28 MB per hour)
            MeetingLibrary._to_m4a(folder / "audio.ogg", folder / "audio.m4a")
        except Exception as exc:  # noqa: BLE001
            log.warning("no AAC copy for %s: %s", folder.name, exc)
    rows = []
    for meta_file in sorted(root.rglob("meta.json")):
        f = meta_file.parent
        if not (f / "notes.json").exists():
            continue
        try:
            if folder is None or f == folder or not (f / MEETING_JS).exists():
                _write(f / MEETING_JS, f"window.meetcapMeeting({_script_json(meeting_record(root, f))});\n")
            meta, notes = json.loads(meta_file.read_text()), json.loads((f / "notes.json").read_text())
        except (OSError, ValueError) as exc:
            log.warning("skipping %s in the reader: %s", f, exc)
            continue
        rows.append({"id": str(meta.get("id") or f.relative_to(root).as_posix()), "folder": f.relative_to(root).as_posix(),
                     "title": notes.get("title") or meta.get("title") or "Untitled meeting",
                     "summary": notes.get("summary", ""), "started_at": meta.get("started_at"),
                     "duration_s": meta.get("duration_s", 0), "app": meta.get("app", ""),
                     "actions": len(notes.get("action_items") or []), "moments": len(notes.get("key_moments") or []),
                     "has_video": any((f / f"recording{x}").exists() for x in (".mp4", ".mov", ".mkv")),
                     "search": _search_text(notes), "v": int((f / MEETING_JS).stat().st_mtime)})
    rows.sort(key=lambda r: r["started_at"] or 0, reverse=True)
    _write(root / STATIC_INDEX, f"window.MEETCAP_INDEX = {_script_json({'built_at': time.time(), 'meetings': rows})};\n")
    html = root / STATIC_HTML
    _write(html, _page({"mode": "static", "built_at": time.time(), "pets": _pet_uris(species or _species())},
                       '<meta name="referrer" content="no-referrer">\n'))
    return html


def _species() -> str:
    try:
        from .config import load_config
        return load_config().ui.pet_species
    except Exception:  # noqa: BLE001
        return "puppy"


def build_reader(link: str, out: Path = READER_FILE, expires: datetime | None = None, species: str | None = None) -> Path:
    """Write the Azure reader with `link` baked in. The file is readable by you only (0600)."""
    info = check_link(link)
    config = {"link": link.strip(), "built_at": time.time(),
              "expires": expires.isoformat() if expires else info["expires"],
              "pets": _pet_uris(species or _species())}
    head = (f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            '<meta name="referrer" content="no-referrer">\n')
    _write(out, _page(config, head), 0o600)
    return out
