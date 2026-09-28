"""ffmpeg/ffprobe helpers. Stateless functions; every output is written to an explicit path."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .models import Frame

log = logging.getLogger(__name__)


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin"):  # launchd has a minimal PATH
        candidate = Path(base) / name
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"{name} not found. Install with: brew install ffmpeg")


def _run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError(f"{Path(args[0]).name} failed ({proc.returncode}): {' | '.join(tail)}")
    return proc


def probe(path: Path) -> dict:
    out = _run([_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path)], timeout=120)
    return json.loads(out.stdout or "{}")


def duration_seconds(path: Path) -> float:
    try:
        return float(probe(path).get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        return 0.0


def has_stream(path: Path, kind: str) -> bool:
    return any(s.get("codec_type") == kind for s in probe(path).get("streams", []))


def wait_until_stable(path: Path, interval: float = 2.0, checks: int = 3, timeout: float = 180.0) -> None:
    """Block until the file size stops changing (OBS finalizes hybrid MP4 after StopRecord returns)."""
    deadline = time.monotonic() + timeout
    last, same = -1, 0
    while time.monotonic() < deadline:
        size = path.stat().st_size if path.exists() else -1
        same = same + 1 if size == last and size > 0 else 0
        if same >= checks - 1:
            return
        last = size
        time.sleep(interval)
    raise TimeoutError(f"{path.name} is still changing after {timeout:.0f}s")


def max_volume_db(path: Path, stream: int | None = None) -> float:
    """Loudest sample in dBFS (about -91 for digital silence). Returns -200 when nothing was decoded."""
    pick = ["-map", f"0:a:{stream}"] if stream is not None else []
    proc = _run([_tool("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path), *pick, "-vn", "-af", "volumedetect",
                 "-f", "null", "-"])
    m = re.search(r"max_volume:\s*(-?[0-9.]+|-inf) dB", proc.stderr or "")
    return float(m.group(1)) if m and m.group(1) != "-inf" else -200.0


def silences(path: Path, noise_db: float = -45.0, min_seconds: float = 0.8) -> list[tuple[float, float]]:
    """Stretches quieter than `noise_db` for at least `min_seconds`, as (start, end) in seconds."""
    proc = _run([_tool("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path), "-vn",
                 "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}", "-f", "null", "-"])
    out: list[tuple[float, float]] = []
    start: float | None = None
    for line in (proc.stderr or "").splitlines():
        if m := re.search(r"silence_start:\s*(-?[0-9.]+)", line):
            start = max(0.0, float(m.group(1)))
        elif (m := re.search(r"silence_end:\s*([0-9.]+)", line)) and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    if start is not None:  # silence runs to the end of the file
        out.append((start, duration_seconds(path) or start))
    return out


def mostly_black(path: Path, samples: int = 5, threshold: int = 16) -> bool:
    """True when every sampled frame is (nearly) black, e.g. OBS had no Screen Recording permission."""
    total = duration_seconds(path)
    if total <= 0 or not has_stream(path, "video"):
        return False
    for i in range(samples):
        t = total * (i + 0.5) / samples
        proc = subprocess.run([_tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
                               "-frames:v", "1", "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"],
                              capture_output=True, timeout=60)
        if proc.returncode == 0 and proc.stdout and max(proc.stdout) >= threshold:
            return False
    return True


def audio_stream_count(path: Path) -> int:
    return sum(1 for s in probe(path).get("streams", []) if s.get("codec_type") == "audio")


def extract_audio(src: Path, dst: Path, bitrate: str = "32k", stream: int | None = None) -> Path:
    """Mono 16 kHz Opus: ~14 MB per hour, well under the fast-transcription 500 MB cap.

    `stream` picks one audio track (OBS writes track 1 = mix, 2 = your mic, 3 = meeting audio).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".partial" + dst.suffix)
    pick = ["-map", f"0:a:{stream}"] if stream is not None else []
    _run([_tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error", "-i", str(src), *pick, "-vn", "-ac", "1",
          "-ar", "16000", "-c:a", "libopus", "-b:a", bitrate, "-application", "voip", str(tmp)])
    os.replace(tmp, dst)
    return dst


def split_audio(src: Path, out_dir: Path, segment_seconds: float) -> list[tuple[Path, float]]:
    """Cut long audio into re-encoded segments. Returns (path, offset_seconds) pairs."""
    total = duration_seconds(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[tuple[Path, float]] = []
    start, i = 0.0, 0
    while start < total:
        part = out_dir / f"part_{i:03d}.ogg"
        _run([_tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
              "-t", f"{segment_seconds:.3f}", "-i", str(src), "-c:a", "libopus", "-b:a", "32k", str(part)])
        parts.append((part, start))
        start += segment_seconds
        i += 1
    return parts


_PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")


def extract_scene_frames(video: Path, out_dir: Path, max_frames: int = 24, threshold: float = 0.2,
                         width: int = 1280) -> list[Frame]:
    """Grab a frame whenever the screen changes noticeably (slide flips, new window shared).

    Falls back to one frame every five minutes when nothing crosses the threshold (static screen).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    vf = f"select='gt(scene,{threshold})',showinfo,scale={width}:-2"
    base = [_tool("ffmpeg"), "-nostdin", "-y", "-i", str(video), "-an", "-vf", vf]
    tail = ["-q:v", "4", str(out_dir / "f_%04d.jpg")]
    try:
        proc = _run(base + ["-fps_mode", "vfr"] + tail)  # ffmpeg >= 5.1
    except RuntimeError as exc:
        if "fps_mode" not in str(exc):
            raise
        proc = _run(base + ["-vsync", "vfr"] + tail)      # older ffmpeg
    times = [float(m.group(1)) for line in proc.stderr.splitlines()
             if "Parsed_showinfo" in line and (m := _PTS_RE.search(line))]
    files = sorted(out_dir.glob("f_*.jpg"))
    frames = [Frame(str(f), int(t * 1000)) for f, t in zip(files, times, strict=False)]

    if not frames:
        proc = _run([_tool("ffmpeg"), "-nostdin", "-y", "-i", str(video), "-an",
                     "-vf", f"fps=1/300,showinfo,scale={width}:-2", "-q:v", "4", str(out_dir / "t_%04d.jpg")])
        times = [float(m.group(1)) for line in proc.stderr.splitlines()
                 if "Parsed_showinfo" in line and (m := _PTS_RE.search(line))]
        frames = [Frame(str(f), int(t * 1000)) for f, t in zip(sorted(out_dir.glob("t_*.jpg")), times, strict=False)]

    if len(frames) > max_frames:  # keep an even spread across the meeting
        step = len(frames) / max_frames
        keep = {int(i * step) for i in range(max_frames)}
        for idx, fr in enumerate(frames):
            if idx not in keep:
                Path(fr.path).unlink(missing_ok=True)
        frames = [fr for idx, fr in enumerate(frames) if idx in keep]
    return frames
