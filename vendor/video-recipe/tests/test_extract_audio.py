"""Tests for scripts.extract_audio.

Builds a 1s tone-generated source video with ffmpeg and runs the real script
on it. Skipped when ffmpeg isn't on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import extract_audio


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def tone_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("audio") / "tone.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=64x48:duration=1,format=yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
    )
    return out


def test_extract_audio_produces_wav(tone_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "audio.wav"
    extract_audio.extract_audio(tone_video, out)
    assert out.exists()
    assert out.stat().st_size > 0
    # WAV magic bytes: RIFF....WAVE
    with out.open("rb") as f:
        head = f.read(12)
    assert head[:4] == b"RIFF"
    assert head[8:12] == b"WAVE"


def test_extract_audio_creates_parent_directory(tone_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "audio.wav"
    extract_audio.extract_audio(tone_video, out)
    assert out.exists()
