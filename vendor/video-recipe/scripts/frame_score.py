"""Score candidate video frames to pick the best Kling image2video reference.

A character reference image only works well if it's sharp and shows a clear,
reasonably-centered face. The naive "grab the 40%-mark frame" heuristic
sometimes lands on a motion-blurred frame or one where the creator's face is
turned away — which produces a weak identity lock in image2video.

This module scores a frame on three signals and combines them:

  - sharpness   — variance of the Laplacian (the standard blur metric). A
                  blurry frame has low high-frequency energy → low variance.
  - face        — largest detected face area as a fraction of the frame, plus
                  a bonus for the face being near frame-center. No face → the
                  face component is 0 (the frame can still win on sharpness if
                  nothing in the batch has a face).
  - (centeredness folds into the face component.)

OpenCV (cv2, shipped as opencv-python-headless — a declared dependency) does
both the Laplacian and the Haar-cascade face detection. cv2 is imported
LAZILY and guarded by `available()`, so decode still runs (falling back to the
midpoint heuristic) in an environment where cv2 isn't importable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def available() -> bool:
    """True iff cv2 (OpenCV) can be imported. Callers fall back to the
    cheap midpoint heuristic when this is False."""
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


def _load_cv2() -> Any:
    import cv2

    return cv2


def laplacian_variance(gray: Any) -> float:
    """Variance of the Laplacian of a grayscale image — the standard
    focus/sharpness measure. Higher = sharper. `gray` is a cv2 (numpy)
    single-channel array."""
    cv2 = _load_cv2()
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return float(lap.var())


@dataclass
class FrameScore:
    """Scoring breakdown for one candidate frame. `total` is what callers
    rank on; the components are kept for logging/debugging + tests."""

    path: Path
    sharpness: float  # raw Laplacian variance
    sharpness_norm: float  # 0..1 within the batch
    face_area: float  # 0..1 fraction of frame covered by the largest face
    face_centered: float  # 0..1, 1 = face centroid at frame center
    has_face: bool
    total: float  # combined score used for ranking


# Component weights. Sharpness is the floor (a blurry ref is always bad);
# face presence + size is the strongest positive signal for a CHARACTER
# reference; centeredness is a light tie-breaker.
_W_SHARP = 0.35
_W_FACE_AREA = 0.50
_W_FACE_CENTER = 0.15


def _detect_largest_face(cv2: Any, gray: Any) -> tuple[float, float] | None:
    """Return (area_fraction, centeredness) of the largest detected face, or
    None if no face is found. centeredness is 1.0 when the face centroid sits
    at the frame center, decaying toward the edges."""
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        return None
    h, w = gray.shape[:2]
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
    if len(faces) == 0:
        return None
    # Largest face by area.
    fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
    area_fraction = (fw * fh) / float(w * h)
    # Centeredness: distance of the face centroid from frame center, scaled by
    # the half-diagonal, inverted to 0..1.
    cx, cy = fx + fw / 2, fy + fh / 2
    dx = (cx - w / 2) / (w / 2)
    dy = (cy - h / 2) / (h / 2)
    dist = (dx * dx + dy * dy) ** 0.5  # 0 at center, ~1.41 at a corner
    centeredness = max(0.0, 1.0 - dist / 1.41421356)
    return min(1.0, area_fraction), centeredness


def score_frames(frame_paths: list[Path], *, prefer_face: bool = True) -> list[FrameScore]:
    """Score each frame in `frame_paths`. Sharpness is normalized across the
    batch (so the comparison is relative — the sharpest frame gets 1.0). When
    `prefer_face` is False, only sharpness drives the score (useful for non-
    talking-head sources where there's no face to find).

    Returns a list of FrameScore in the SAME order as the input. Frames that
    cv2 can't read are scored 0. Empty input → empty list. Requires cv2; call
    `available()` first."""
    cv2 = _load_cv2()
    raw: list[dict] = []
    for p in frame_paths:
        img = cv2.imread(str(p))
        if img is None:
            raw.append({"path": p, "sharp": 0.0, "face": None})
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharp = laplacian_variance(gray)
        face = _detect_largest_face(cv2, gray) if prefer_face else None
        raw.append({"path": p, "sharp": sharp, "face": face})

    max_sharp = max((r["sharp"] for r in raw), default=0.0) or 1.0
    scores: list[FrameScore] = []
    for r in raw:
        sharp_norm = r["sharp"] / max_sharp
        if r["face"] is not None:
            face_area, face_center = r["face"]
            has_face = True
        else:
            face_area, face_center = 0.0, 0.0
            has_face = False
        if prefer_face:
            total = (
                _W_SHARP * sharp_norm
                + _W_FACE_AREA * face_area
                + _W_FACE_CENTER * face_center
            )
        else:
            total = sharp_norm
        scores.append(
            FrameScore(
                path=r["path"],
                sharpness=r["sharp"],
                sharpness_norm=sharp_norm,
                face_area=face_area,
                face_centered=face_center,
                has_face=has_face,
                total=total,
            )
        )
    return scores


def pick_best_frame(frame_paths: list[Path], *, prefer_face: bool = True) -> FrameScore | None:
    """Score `frame_paths` and return the highest-scoring FrameScore, or None
    when the list is empty. Requires cv2 (`available()`)."""
    scores = score_frames(frame_paths, prefer_face=prefer_face)
    if not scores:
        return None
    return max(scores, key=lambda s: s.total)
