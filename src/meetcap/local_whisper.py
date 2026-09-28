"""On-Mac transcription with Whisper on Apple Silicon (mlx-whisper).

Runs in a short-lived subprocess so the ~1.6 GB model is released from memory as soon as a meeting
is transcribed; the long-running menu bar agent stays small.

    python -m meetcap.local_whisper IN.ogg OUT.json [--model REPO] [--language en] [--prompt "..."]
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def supported() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "on-Mac transcription needs macOS"
    if platform.machine() != "arm64":
        return False, "on-Mac transcription needs an Apple Silicon Mac (M1 or newer)"
    return True, ""


def installed() -> bool:
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - ImportError, or mlx failing to load Metal
        return False


def install() -> None:
    """pip install mlx-whisper into this virtualenv."""
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "mlx-whisper>=0.4"], check=True)


def run(audio: Path, out: Path, model: str = DEFAULT_MODEL, language: str | None = None,
        prompt: str | None = None, timeout: float = 4 * 3600) -> dict:
    """Transcribe in a subprocess and return the parsed JSON result ({text, segments, language})."""
    args = [sys.executable, "-m", "meetcap.local_whisper", str(audio), str(out), "--model", model]
    if language:
        args += ["--language", language]
    if prompt:
        args += ["--prompt", prompt]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-4:])
        raise RuntimeError(f"local transcription failed: {tail}")
    return json.loads(out.read_text())


def _main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--language")
    ap.add_argument("--prompt")
    a = ap.parse_args(argv)

    import mlx_whisper

    kwargs = {"path_or_hf_repo": a.model, "condition_on_previous_text": False, "verbose": None}
    if a.language:
        kwargs["language"] = a.language
    if a.prompt:
        kwargs["initial_prompt"] = a.prompt
    result = mlx_whisper.transcribe(a.audio, **kwargs)
    segments = [{"start": float(s.get("start", 0)), "end": float(s.get("end", 0)), "text": s.get("text", ""),
                 "no_speech_prob": float(s.get("no_speech_prob", 0) or 0)} for s in result.get("segments", [])]
    Path(a.out).write_text(json.dumps({"text": result.get("text", ""), "language": result.get("language"),
                                       "segments": segments}))


if __name__ == "__main__":
    _main()
