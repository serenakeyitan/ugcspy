"""Tests for scripts.detect_cuts.

Builds a synthetic 6s mp4 with three 2s color blocks (red/green/blue) so we can
assert the detector finds the obvious cuts. ffmpeg is required at test time;
the test is skipped if it's not available.

scenedetect is a declared core dependency, but the import is lazy (inside
detect_cuts() to avoid loading opencv at import time). Skip the whole module
when it isn't installed so a partial dev environment doesn't hard-fail with
ModuleNotFoundError — same tolerance the suite already gives missing ffmpeg /
tesseract / whisper. A full `pip install -e .` installs it and runs these for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("scenedetect")

from scripts import detect_cuts


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def three_cuts_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")

    out = tmp_path_factory.mktemp("video") / "three_cuts.mp4"
    parts = []
    for color in ("red", "green", "blue"):
        part = out.parent / f"{color}.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:size=320x240:duration=2,format=yuv420p",
                "-c:v",
                "libx264",
                "-r",
                "24",
                str(part),
            ],
            check=True,
        )
        parts.append(part)

    list_file = out.parent / "list.txt"
    list_file.write_text("\n".join(f"file '{p}'" for p in parts))
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(out),
        ],
        check=True,
    )
    return out


def test_detect_three_cuts(three_cuts_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "cuts.json"
    cuts = detect_cuts.detect_cuts(three_cuts_video, out)

    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk == cuts

    # Detector should find 3 cuts (one per color block).
    assert len(cuts) == 3, f"expected 3 cuts, got {len(cuts)}: {cuts}"

    # Indices contiguous, 0-based.
    assert [c["index"] for c in cuts] == [0, 1, 2]

    # Total covered duration ~= 6s (within 0.5s — concat demuxer can introduce
    # ~0.25s timing slack with re-encoded segments).
    total = cuts[-1]["end_sec"] - cuts[0]["start_sec"]
    assert abs(total - 6.0) < 0.5, f"total duration off: {total}"

    # Each cut ~2s — none should be flagged short.
    for c in cuts:
        assert not c["flagged_short"]
        assert 1.5 < c["duration_sec"] < 2.5


def test_detect_cuts_writes_json_directory_if_missing(
    three_cuts_video: Path, tmp_path: Path
) -> None:
    out = tmp_path / "nested" / "subdir" / "cuts.json"
    detect_cuts.detect_cuts(three_cuts_video, out)
    assert out.exists()


def test_adaptive_detector_also_finds_cuts(three_cuts_video: Path, tmp_path: Path) -> None:
    """AdaptiveDetector path is wired up correctly and finds the obvious 3-color cuts."""
    out = tmp_path / "cuts.json"
    cuts = detect_cuts.detect_cuts(three_cuts_video, out, threshold=3.0, detector="adaptive")
    # On the 3-color synthetic video both detectors should find 3 cuts.
    assert len(cuts) == 3
    assert [c["index"] for c in cuts] == [0, 1, 2]


def test_unknown_detector_raises(three_cuts_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "cuts.json"
    with pytest.raises(ValueError, match="unknown detector"):
        detect_cuts.detect_cuts(three_cuts_video, out, detector="bogus")
