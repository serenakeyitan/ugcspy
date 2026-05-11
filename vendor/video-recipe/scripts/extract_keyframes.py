"""Extract 3 keyframes per cut at 10%/50%/90% of the cut duration.

Reads ``cuts.json`` produced by ``scripts.detect_cuts`` and writes
``<out_dir>/<cut_index>/{a,b,c}.jpg``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

FRAME_NAMES = ("a", "b", "c")
FRAME_FRACTIONS = (0.10, 0.50, 0.90)


def _ffmpeg_bin() -> str:
    bin_path = shutil.which("ffmpeg")
    if not bin_path:
        raise RuntimeError("ffmpeg not found on PATH")
    return bin_path


def extract_one_frame(video_path: Path, ts_sec: float, out_path: Path) -> None:
    """Extract a single frame at ts_sec into out_path. ffmpeg required."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # -ss before -i seeks fast (keyframe-aligned, then accurate); good enough for stills.
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{ts_sec:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",  # high-quality JPEG
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced empty frame at {out_path}")


def extract_keyframes(
    video_path: Path,
    cuts_json: Path,
    out_dir: Path,
) -> dict[int, list[Path]]:
    """For each cut in cuts_json, extract 3 keyframes into out_dir/<index>/.

    Returns a map of cut_index -> list of frame paths in (a, b, c) order.
    """
    cuts: list[dict[str, Any]] = json.loads(cuts_json.read_text())
    result: dict[int, list[Path]] = {}

    for cut in cuts:
        index = int(cut["index"])
        start = float(cut["start_sec"])
        duration = float(cut["duration_sec"])

        cut_dir = out_dir / str(index)
        frame_paths: list[Path] = []
        for name, frac in zip(FRAME_NAMES, FRAME_FRACTIONS, strict=True):
            ts = start + frac * duration
            frame_path = cut_dir / f"{name}.jpg"
            extract_one_frame(video_path, ts, frame_path)
            frame_paths.append(frame_path)
        result[index] = frame_paths

    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract keyframes per cut.")
    parser.add_argument("video_path", type=Path)
    parser.add_argument("cuts_json", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("extract_keyframes"):
        result = extract_keyframes(args.video_path, args.cuts_json, args.out_dir)
    n_frames = sum(len(v) for v in result.values())
    print(f"{n_frames} frames across {len(result)} cuts -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
