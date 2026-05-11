"""Tests for scripts.detect_audio_cuts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import detect_audio_cuts


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def silence_audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """6-second WAV: 2s tone, 1s silence, 2s tone, 1s silence."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("audiocut") / "tone_gap.wav"
    # Build via concat of segments (lavfi sine + silence).
    parts: list[Path] = []
    for i, spec in enumerate(
        [
            "sine=frequency=440:duration=2",
            "anullsrc=channel_layout=mono:sample_rate=16000:duration=1",
            "sine=frequency=440:duration=2",
            "anullsrc=channel_layout=mono:sample_rate=16000:duration=1",
        ]
    ):
        part = out.parent / f"part{i}.wav"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                spec,
                "-ar",
                "16000",
                "-ac",
                "1",
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


def test_detect_silence_finds_two_boundaries(silence_audio: Path) -> None:
    """Tone-gap-tone-gap WAV: silencedetect should find two silence_end events."""
    boundaries = detect_audio_cuts.detect_silence_boundaries(silence_audio)
    # We have silence at 2s-3s and 5s-6s. silencedetect reports the END of each
    # silent stretch (when audio resumes), so we expect ~3.0s and the file end
    # may or may not be reported depending on ffmpeg version's handling of EOF.
    assert len(boundaries) >= 1
    # Ensure the first boundary lands near 3.0s (end of first silence).
    first_end = boundaries[0]["end_sec"]
    assert 2.8 < first_end < 3.3, f"expected silence_end near 3.0s, got {first_end}"


def test_merge_silence_splits_lumped_cut() -> None:
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 6.0, "duration_sec": 6.0, "flagged_short": False},
    ]
    silences = [{"end_sec": 3.0, "duration_sec": 1.0}]
    merged = detect_audio_cuts.merge_silence_into_cuts(cuts, silences)
    assert len(merged) == 2
    assert merged[0]["start_sec"] == 0.0
    assert merged[0]["end_sec"] == 3.0
    assert merged[0]["index"] == 0
    assert merged[1]["start_sec"] == 3.0
    assert merged[1]["end_sec"] == 6.0
    assert merged[1]["index"] == 1
    assert merged[1]["split_by_audio"] is True


def test_merge_silence_ignores_boundary_at_existing_cut_edge() -> None:
    """Silence boundary near an existing pixel-cut edge should NOT split."""
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 3.0, "duration_sec": 3.0, "flagged_short": False},
        {"index": 1, "start_sec": 3.0, "end_sec": 6.0, "duration_sec": 3.0, "flagged_short": False},
    ]
    # Silence boundary at 3.05 (just past the pixel cut at 3.0)
    silences = [{"end_sec": 3.05, "duration_sec": 1.0}]
    merged = detect_audio_cuts.merge_silence_into_cuts(cuts, silences)
    # Boundary is within epsilon (0.1s) of the existing cut edge — no split.
    assert len(merged) == 2


def test_merge_silence_ignores_when_no_silences() -> None:
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 6.0, "duration_sec": 6.0, "flagged_short": False},
    ]
    merged = detect_audio_cuts.merge_silence_into_cuts(cuts, [])
    assert merged == cuts


def test_merge_silence_handles_multiple_splits_in_one_cut() -> None:
    cuts = [
        {
            "index": 0,
            "start_sec": 0.0,
            "end_sec": 10.0,
            "duration_sec": 10.0,
            "flagged_short": False,
        },
    ]
    silences = [
        {"end_sec": 3.0, "duration_sec": 0.5},
        {"end_sec": 6.0, "duration_sec": 0.5},
    ]
    merged = detect_audio_cuts.merge_silence_into_cuts(cuts, silences)
    assert len(merged) == 3
    assert [c["start_sec"] for c in merged] == [0.0, 3.0, 6.0]
    assert [c["end_sec"] for c in merged] == [3.0, 6.0, 10.0]
    assert merged[0]["split_by_audio"] is False  # first sub-cut keeps original boundary
    assert merged[1]["split_by_audio"] is True
    assert merged[2]["split_by_audio"] is True
