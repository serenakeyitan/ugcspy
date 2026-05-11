"""Detect audio-level boundaries (silence transitions) and merge with pixel cuts.

Pixel-difference cut detection (PySceneDetect) misses scene changes when
consecutive AI generations share a color palette. Edited videos almost always
have an audio cut — a moment of silence or level drop — at every edit point.
Layering the two signals gives us cuts the pixel detector merged.

Usage:

  python -m scripts.detect_audio_cuts <audio.wav> <silence.json>
    Run ffmpeg silencedetect, write silence boundaries.

  python -m scripts.detect_audio_cuts <audio.wav> <silence.json> \\
    --merge-with <cuts.json> --out <merged_cuts.json>
    Also merge silence boundaries into cuts.json: any pixel cut whose interior
    contains a silence boundary gets split there.

ffmpeg required.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_SILENCE_DB = -30.0
DEFAULT_MIN_SILENCE_SEC = 0.18
SHORT_CUT_THRESHOLD_SEC = 0.4

_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


def _ffmpeg_bin() -> str:
    bin_path = shutil.which("ffmpeg")
    if not bin_path:
        raise RuntimeError("ffmpeg not found on PATH")
    return bin_path


def detect_silence_boundaries(
    audio_path: Path,
    *,
    silence_db: float = DEFAULT_SILENCE_DB,
    min_silence_sec: float = DEFAULT_MIN_SILENCE_SEC,
) -> list[dict[str, Any]]:
    """Run ffmpeg silencedetect and return a list of silence-end timestamps.

    Each entry is ``{"end_sec": float, "duration_sec": float}``. The end of a
    silent stretch is the cut point we care about (audio resumes => new scene).
    """
    cmd = [
        _ffmpeg_bin(),
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={silence_db}dB:d={min_silence_sec}",
        "-f",
        "null",
        "-",
    ]
    # silencedetect logs to stderr.
    result = subprocess.run(cmd, capture_output=True, check=True)
    stderr = result.stderr.decode("utf-8", errors="replace")
    boundaries: list[dict[str, Any]] = []
    for match in _SILENCE_END_RE.finditer(stderr):
        end_sec = float(match.group(1))
        duration = float(match.group(2))
        boundaries.append({"end_sec": round(end_sec, 3), "duration_sec": round(duration, 3)})
    return boundaries


def merge_silence_into_cuts(
    cuts: list[dict[str, Any]],
    silences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """For any pixel cut whose interior contains one or more silence_end
    timestamps, split the cut at those timestamps. Returns a re-indexed list.

    We split only on silences that fall strictly inside a cut (not within the
    first or last 100ms — those are already at-or-near the existing cut
    boundaries).
    """
    epsilon = 0.1
    out: list[dict[str, Any]] = []
    silence_times = sorted(s["end_sec"] for s in silences)

    for cut in cuts:
        start = float(cut["start_sec"])
        end = float(cut["end_sec"])
        # Splits inside (start + epsilon, end - epsilon).
        split_points = [t for t in silence_times if start + epsilon < t < end - epsilon]
        if not split_points:
            out.append(dict(cut))
            continue
        # Build sub-cuts.
        boundaries = [start, *split_points, end]
        for i in range(len(boundaries) - 1):
            sub_start = boundaries[i]
            sub_end = boundaries[i + 1]
            duration = sub_end - sub_start
            sub = dict(cut)
            sub.pop("index", None)
            sub.update(
                start_sec=round(sub_start, 3),
                end_sec=round(sub_end, 3),
                duration_sec=round(duration, 3),
                flagged_short=duration < SHORT_CUT_THRESHOLD_SEC,
                split_by_audio=i > 0,
            )
            out.append(sub)

    # Re-index 0-based.
    for i, c in enumerate(out):
        c["index"] = i
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Detect audio silence boundaries and optionally merge with pixel cuts."
    )
    parser.add_argument("audio_path", type=Path)
    parser.add_argument("silence_out", type=Path, help="Where to write silence boundaries JSON")
    parser.add_argument("--silence-db", type=float, default=DEFAULT_SILENCE_DB)
    parser.add_argument("--min-silence-sec", type=float, default=DEFAULT_MIN_SILENCE_SEC)
    parser.add_argument(
        "--merge-with",
        type=Path,
        default=None,
        help="Pixel cuts.json. If set, also write merged cuts to --out.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Path to write merged cuts JSON when --merge-with is used.",
    )
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("detect_audio_cuts.silencedetect"):
        silences = detect_silence_boundaries(
            args.audio_path,
            silence_db=args.silence_db,
            min_silence_sec=args.min_silence_sec,
        )
        args.silence_out.parent.mkdir(parents=True, exist_ok=True)
        args.silence_out.write_text(json.dumps(silences, indent=2))
    print(f"silence boundaries: {len(silences)} -> {args.silence_out}")

    if args.merge_with:
        if not args.out:
            parser.error("--merge-with requires --out")
        with stage("detect_audio_cuts.merge"):
            pixel_cuts = json.loads(args.merge_with.read_text())
            merged = merge_silence_into_cuts(pixel_cuts, silences)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(merged, indent=2))
        added = len(merged) - len(pixel_cuts)
        print(f"merged: {len(pixel_cuts)} -> {len(merged)} cuts (+{added}) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
