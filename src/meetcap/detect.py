"""Meeting detection: which meeting app holds the microphone, debounced into start/stop events."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Protocol

log = logging.getLogger(__name__)


class MicSource(Protocol):
    def active_bundles(self) -> set[str]: ...


class MicWatch:
    """Adapter over the `micwatch` Swift helper (Core Audio per-process input flags)."""

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = Path(path)
        self.timeout = timeout

    def available(self) -> bool:
        return self.path.exists()

    def processes(self, include_all: bool = False) -> list[dict]:
        args = [str(self.path)] + (["--all"] if include_all else [])
        out = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout, check=True)
        return json.loads(out.stdout.strip() or "[]")

    def active_bundles(self) -> set[str]:
        return {p["bundle"] for p in self.processes() if p.get("input", True) and p.get("bundle")}


def match_app(bundles: Iterable[str], prefixes: Iterable[str]) -> str | None:
    """Return the configured prefix that one of `bundles` belongs to.

    `com.microsoft.teams2.helper` matches `com.microsoft.teams2`; `com.microsoft.teams2` does not match
    `com.microsoft.teams` (the prefix must be followed by '.' or end the string).
    """
    prefixes = list(prefixes)
    for b in sorted(bundles):
        for p in prefixes:
            if b == p or b.startswith(p + "."):
                return p
    return None


@dataclass(frozen=True)
class DetectorEvent:
    kind: Literal["start", "stop"]
    app: str
    at: float  # when the underlying change happened (before debounce)


class MeetingDetector:
    """Pure state machine. Feed it the matched app (or None) on every poll; it emits debounced events.

    start: a meeting app has held the mic for `start_after` seconds.
    stop:  no meeting app has held the mic for `stop_after` seconds.
    """

    def __init__(self, start_after: float, stop_after: float):
        self.start_after = start_after
        self.stop_after = stop_after
        self.active_app: str | None = None
        self._candidate: str | None = None
        self._seen_since: float | None = None
        self._gone_since: float | None = None

    def reset(self) -> None:
        self.active_app = None
        self._candidate = self._seen_since = self._gone_since = None

    def update(self, app: str | None, now: float) -> DetectorEvent | None:
        if self.active_app is None:
            if not app:
                self._candidate = self._seen_since = None
                return None
            if app != self._candidate or self._seen_since is None:
                self._candidate, self._seen_since = app, now
            if now - self._seen_since >= self.start_after:
                self.active_app, self._gone_since = app, None
                return DetectorEvent("start", app, self._seen_since)
            return None

        if app:
            self._gone_since = None
            return None
        if self._gone_since is None:
            self._gone_since = now
        if now - self._gone_since >= self.stop_after:
            ended, at = self.active_app, self._gone_since
            self.reset()
            return DetectorEvent("stop", ended, at)
        return None
