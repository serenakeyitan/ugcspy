"""Tests for decode.extract_reference_frame — the image2video character
reference keyframe (#25). Uses a synthetic ffmpeg-generated clip; skips
when ffmpeg is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import decode

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def synthetic_clip(tmp_path: Path) -> Path:
    """A 4s solid-color 320x568 (9:16-ish) clip."""
    out = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=purple:320x568:duration=4:rate=24",
            "-pix_fmt", "yuv420p", str(out),
        ],
        check=True,
    )
    return out


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not on PATH")
def test_extract_reference_frame_writes_jpeg(synthetic_clip, tmp_path):
    out = tmp_path / "reference.jpg"
    result = decode.extract_reference_frame(synthetic_clip, out, duration_sec=4.0)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0
    # It's a JPEG (starts with the JPEG SOI marker 0xFFD8).
    assert out.read_bytes()[:2] == b"\xff\xd8"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not on PATH")
def test_extract_reference_frame_native_resolution(synthetic_clip, tmp_path):
    """The reference must NOT be downscaled — Kling wants full detail for
    identity fidelity. Confirm the output is the source's 320x568."""
    out = tmp_path / "reference.jpg"
    decode.extract_reference_frame(synthetic_clip, out, duration_sec=4.0)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip().replace(" ", "") == "320,568"


def test_extract_reference_frame_returns_none_on_bad_input(tmp_path):
    """A missing/unreadable source should return None (decode continues —
    the character reference is optional), not crash."""
    out = tmp_path / "reference.jpg"
    result = decode.extract_reference_frame(tmp_path / "nonexistent.mp4", out, duration_sec=4.0)
    assert result is None
    assert not out.exists()
