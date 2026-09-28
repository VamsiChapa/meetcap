import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Keep tests away from the real ~/Library locations.
_TMP_HOME = Path(os.environ.setdefault("MEETCAP_HOME", str(Path(__file__).parent / ".tmp_home")))
os.environ.setdefault("MEETCAP_LOG_DIR", str(_TMP_HOME / "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """12 s clip: a moving test pattern that changes scene at 6 s, plus a sine tone."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path / "rec.mp4"
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=6",
        "-f", "lavfi", "-i", "smptebars=size=640x360:rate=10:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(out),
    ], check=True)
    return out
