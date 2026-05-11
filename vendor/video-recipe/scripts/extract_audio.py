"""Extract a 16 kHz mono PCM WAV audio track from a video.

The output format is what Whisper expects natively, which keeps the
``transcribe.py`` pipeline simple. ffmpeg required.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _ffmpeg_bin() -> str:
    bin_path = shutil.which("ffmpeg")
    if not bin_path:
        raise RuntimeError("ffmpeg not found on PATH")
    return bin_path


def extract_audio(video_path: Path, out_path: Path) -> Path:
    """Extract mono 16 kHz PCM WAV from ``video_path`` to ``out_path``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced empty audio file at {out_path}")
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract audio from a video.")
    parser.add_argument("video_path", type=Path)
    parser.add_argument("out_path", type=Path)
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("extract_audio"):
        out = extract_audio(args.video_path, args.out_path)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
