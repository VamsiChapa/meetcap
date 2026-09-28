"""macOS user notifications via osascript. No-op (log only) elsewhere."""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return " ".join(str(s).split()).replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str, sound: bool = False) -> None:
    log.info("notify: %s: %s", title, message)
    if sys.platform != "darwin":
        return
    script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
    if sound:
        script += ' sound name "Submarine"'
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("notification failed: %s", exc)
