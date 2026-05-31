"""Tests for scripts.frame_score + decode.extract_reference_frame's scored
pick. cv2 (OpenCV) is a declared dep but optional at runtime, so the
cv2-dependent tests skip when it's unavailable; the pure pieces (candidate
seek-time math, graceful fallback when cv2 is missing) always run.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from scripts import decode, frame_score

_HAS_CV2 = frame_score.available()
_HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ─── _candidate_seek_times (pure math, no cv2/ffmpeg) ───────────────────────


def test_candidate_seek_times_stay_in_middle_band():
    seeks = decode._candidate_seek_times(60.0, max_samples=8)
    assert len(seeks) >= 2
    # All samples inside the 20%-80% band (12s..48s for a 60s clip).
    assert all(60.0 * 0.2 <= t <= 60.0 * 0.8 + 1e-6 for t in seeks)
    # Sorted, spanning the band.
    assert seeks == sorted(seeks)
    assert seeks[0] == pytest.approx(12.0, abs=0.5)
    assert seeks[-1] == pytest.approx(48.0, abs=0.5)


def test_candidate_seek_times_short_video_uses_midpoint():
    # A 4s clip can't sustain a multi-sample middle band → single midpoint.
    seeks = decode._candidate_seek_times(4.0)
    assert seeks == [pytest.approx(1.6, abs=0.01)]  # 4 * 0.4


def test_candidate_seek_times_zero_duration():
    assert decode._candidate_seek_times(0.0) == [0.0]


def test_candidate_seek_times_capped_by_max_samples():
    seeks = decode._candidate_seek_times(600.0, max_samples=8)
    assert len(seeks) <= 8


# ─── graceful fallback when cv2 is unavailable ──────────────────────────────


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not on PATH")
def test_extract_reference_frame_falls_back_without_cv2(tmp_path, monkeypatch):
    """When cv2 is unavailable, extract_reference_frame must still produce a
    frame via the 40%-mark fallback (no crash)."""
    monkeypatch.setattr(frame_score, "available", lambda: False)
    clip = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=teal:320x568:duration=6:rate=24", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    out = tmp_path / "reference.jpg"
    result = decode.extract_reference_frame(clip, out, duration_sec=6.0)
    assert result == out
    assert out.exists() and out.read_bytes()[:2] == b"\xff\xd8"  # JPEG


# ─── cv2-dependent scoring ──────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_CV2, reason="OpenCV (cv2) not installed")
def test_laplacian_variance_sharp_beats_blurry(tmp_path):
    import cv2
    import numpy as np

    # A high-frequency checkerboard (sharp) vs a uniform gray (blurry/flat).
    sharp = np.indices((200, 200)).sum(axis=0) % 2 * 255
    sharp = sharp.astype("uint8")
    flat = np.full((200, 200), 127, dtype="uint8")
    assert frame_score.laplacian_variance(sharp) > frame_score.laplacian_variance(flat)
    # Blurring the sharp image should lower its variance.
    blurred = cv2.GaussianBlur(sharp, (9, 9), 0)
    assert frame_score.laplacian_variance(blurred) < frame_score.laplacian_variance(sharp)


@pytest.mark.skipif(not _HAS_CV2, reason="OpenCV (cv2) not installed")
def test_score_frames_ranks_sharper_higher(tmp_path):
    import cv2
    import numpy as np

    sharp_img = (np.indices((300, 300)).sum(axis=0) % 2 * 255).astype("uint8")
    blur_img = cv2.GaussianBlur(sharp_img, (21, 21), 0)
    sharp_p = tmp_path / "sharp.jpg"
    blur_p = tmp_path / "blur.jpg"
    cv2.imwrite(str(sharp_p), sharp_img)
    cv2.imwrite(str(blur_p), blur_img)

    # prefer_face=False isolates the sharpness signal (no faces in these).
    scores = frame_score.score_frames([sharp_p, blur_p], prefer_face=False)
    by_path = {s.path: s for s in scores}
    assert by_path[sharp_p].total > by_path[blur_p].total
    best = frame_score.pick_best_frame([blur_p, sharp_p], prefer_face=False)
    assert best.path == sharp_p


@pytest.mark.skipif(not _HAS_CV2, reason="OpenCV (cv2) not installed")
def test_score_frames_unreadable_image_scores_zero(tmp_path):
    bad = tmp_path / "not-an-image.jpg"
    bad.write_bytes(b"garbage")
    scores = frame_score.score_frames([bad], prefer_face=False)
    assert len(scores) == 1
    assert scores[0].total == 0.0
    assert scores[0].has_face is False


def test_score_frames_empty_list():
    # Empty input is safe even without cv2 reaching the loop.
    if not _HAS_CV2:
        pytest.skip("cv2 not installed")
    assert frame_score.score_frames([]) == []
    assert frame_score.pick_best_frame([]) is None
