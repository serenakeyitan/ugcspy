"""Tests for scripts.extract_keyframes.

Reuses the synthetic 3-color video from test_detect_cuts (built fresh per
test module), runs detection, then verifies keyframe extraction produces
3 valid JPEGs per cut.

Runs detect_cuts.detect_cuts(), which lazily imports scenedetect (a declared
core dep). Skip the whole module when scenedetect isn't installed so a partial
dev environment doesn't hard-fail with ModuleNotFoundError.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("scenedetect")

from scripts import detect_cuts, extract_keyframes


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def three_cuts_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")

    out = tmp_path_factory.mktemp("kfvideo") / "three_cuts.mp4"
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


def _is_valid_jpeg(path: Path) -> bool:
    """Quick magic-byte check — JPEGs start with FFD8FF."""
    with path.open("rb") as f:
        return f.read(3) == b"\xff\xd8\xff"


def test_extract_three_frames_per_cut(three_cuts_video: Path, tmp_path: Path) -> None:
    cuts_json = tmp_path / "cuts.json"
    detect_cuts.detect_cuts(three_cuts_video, cuts_json)

    out_dir = tmp_path / "cuts"
    result = extract_keyframes.extract_keyframes(three_cuts_video, cuts_json, out_dir)

    cuts_data = json.loads(cuts_json.read_text())
    assert set(result.keys()) == {c["index"] for c in cuts_data}

    for index, frame_paths in result.items():
        assert len(frame_paths) == 3
        cut_dir = out_dir / str(index)
        for name, path in zip(("a", "b", "c"), frame_paths, strict=True):
            assert path == cut_dir / f"{name}.jpg"
            assert path.exists()
            assert path.stat().st_size > 0
            assert _is_valid_jpeg(path), f"{path} is not a valid JPEG"


def test_extract_keyframes_uses_correct_timestamps(
    three_cuts_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the requested timestamps are 10/50/90% of each cut's duration."""
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 2.0, "duration_sec": 2.0, "flagged_short": False},
    ]
    cuts_json = tmp_path / "cuts.json"
    cuts_json.write_text(json.dumps(cuts))

    timestamps: list[float] = []

    def fake_extract(video_path: Path, ts_sec: float, out_path: Path) -> None:
        timestamps.append(ts_sec)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Write valid-enough JPEG header so downstream checks pass if any.
        out_path.write_bytes(b"\xff\xd8\xff\xe0fake")

    monkeypatch.setattr(extract_keyframes, "extract_one_frame", fake_extract)
    extract_keyframes.extract_keyframes(three_cuts_video, cuts_json, tmp_path / "cuts")

    assert timestamps == [0.2, 1.0, 1.8]
