"""Detect cuts in a video and write cuts.json.

Two detector backends:

- ``content`` (default) — PySceneDetect ``ContentDetector`` at a fixed threshold.
  Best when scene changes have clear color/luminance contrast.
- ``adaptive`` — PySceneDetect ``AdaptiveDetector``. Normalizes against a rolling
  contrast window; better for videos with sustained color palettes (e.g. fast
  TikTok-style AI montages where consecutive AI generations share a teal-orange
  grade and the fixed detector merges them).

Output schema is a JSON array of:

    {"index": int, "start_sec": float, "end_sec": float,
     "duration_sec": float, "flagged_short": bool}

Cuts shorter than ``SHORT_CUT_THRESHOLD_SEC`` are flagged but not dropped — the
caller decides how to handle them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SHORT_CUT_THRESHOLD_SEC = 0.4

# Recommended thresholds by video category, surfaced in SKILL.md.
THRESHOLD_GUIDANCE = {
    "announcement_reel": 15.0,
    "tiktok_montage": 18.0,
    "default": 27.0,
    "slow_cinematic": 30.0,
}


def _build_detector(detector: str, threshold: float) -> Any:
    from scenedetect import AdaptiveDetector, ContentDetector

    if detector == "content":
        return ContentDetector(threshold=threshold)
    if detector == "adaptive":
        # AdaptiveDetector takes adaptive_threshold (default 3.0). Map our
        # --threshold flag onto it so the CLI surface stays simple.
        return AdaptiveDetector(adaptive_threshold=threshold)
    raise ValueError(f"unknown detector: {detector!r} (expected 'content' or 'adaptive')")


def detect_cuts(
    video_path: Path,
    out_path: Path,
    threshold: float = 27.0,
    detector: str = "content",
) -> list[dict[str, Any]]:
    """Run PySceneDetect on ``video_path`` and write cuts.json to ``out_path``.

    ``detector`` is ``"content"`` (default) or ``"adaptive"``. For ``adaptive``,
    the typical useful range for ``threshold`` is 2.0-4.0 (default 3.0); for
    ``content`` it's 15-30 (default 27).

    Returns the cut list (also written to disk).
    """
    # Lazy import — keeps test fakes simple and avoids opencv at import time.
    from scenedetect import SceneManager, open_video

    video = open_video(str(video_path))
    scene_manager = SceneManager()
    scene_manager.add_detector(_build_detector(detector, threshold))
    scene_manager.detect_scenes(video)
    scenes = scene_manager.get_scene_list()

    cuts: list[dict[str, Any]] = []
    if scenes:
        for index, (start, end) in enumerate(scenes):
            start_sec = float(start.seconds)
            end_sec = float(end.seconds)
            duration = end_sec - start_sec
            cuts.append(
                {
                    "index": index,
                    "start_sec": round(start_sec, 3),
                    "end_sec": round(end_sec, 3),
                    "duration_sec": round(duration, 3),
                    "flagged_short": duration < SHORT_CUT_THRESHOLD_SEC,
                }
            )
    else:
        # No detected scene boundaries — treat the whole video as one cut.
        duration = float(video.duration.seconds)
        cuts.append(
            {
                "index": 0,
                "start_sec": 0.0,
                "end_sec": round(duration, 3),
                "duration_sec": round(duration, 3),
                "flagged_short": duration < SHORT_CUT_THRESHOLD_SEC,
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cuts, indent=2))
    return cuts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Detect cuts in a video.")
    parser.add_argument("video_path", type=Path)
    parser.add_argument("out_path", type=Path)
    parser.add_argument(
        "--threshold",
        type=float,
        default=27.0,
        help=(
            "Detector threshold. For --detector content (default), 15-30 is "
            "useful (default 27.0; lower = more sensitive). For --detector "
            "adaptive, 2.0-4.0 is useful (try 3.0)."
        ),
    )
    parser.add_argument(
        "--detector",
        choices=("content", "adaptive"),
        default="content",
        help=(
            "Cut detector backend. 'content' is fixed-threshold (good default). "
            "'adaptive' normalizes against a rolling contrast window and "
            "typically catches more cuts in videos with sustained color "
            "palettes (e.g. TikTok-style AI montages with a teal-orange grade)."
        ),
    )
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("detect_cuts"):
        cuts = detect_cuts(args.video_path, args.out_path, args.threshold, args.detector)
    print(f"{len(cuts)} cuts -> {args.out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
