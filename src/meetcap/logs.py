"""Structured JSON logging to ~/Library/Logs/meetcap/meetcap.log (rotated) plus stderr."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time

from .config import LOG_DIR


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(verbose: bool = False, to_file: bool = True) -> None:
    root = logging.getLogger()
    if getattr(root, "_meetcap_configured", False):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if to_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG_DIR / "meetcap.log", maxBytes=5_000_000, backupCount=5)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    for noisy in ("azure", "urllib3", "httpx", "openai", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._meetcap_configured = True  # type: ignore[attr-defined]
