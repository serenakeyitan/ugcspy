"""Tests for scripts.ocr_title_cards.

Builds a synthetic title-card image with PIL (white background + black text)
and a synthetic photo-like image (random noise) and asserts the classifier
distinguishes them.

Skipped if tesseract isn't on PATH.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import ocr_title_cards


def _tesseract() -> str | None:
    return shutil.which("tesseract")


@pytest.fixture(scope="module")
def title_card_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _tesseract():
        pytest.skip("tesseract not available")
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    out = tmp_path_factory.mktemp("ocr") / "title_card.jpg"
    img = Image.new("RGB", (1280, 720), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Pick the first usable TTF we can find. Pillow's load_default returns a
    # tiny bitmap font that tesseract reads with low confidence, which would
    # break the title-card classifier on CI. Try real TTFs first.
    font = None
    for candidate in (
        "/System/Library/Fonts/Helvetica.ttc",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Ubuntu
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 60)
            break
        except OSError:
            continue
    if font is None:
        pytest.skip("no usable TTF font found for OCR test")
    draw.text((150, 320), "A cartoon kangaroo disco dances", fill=(0, 0, 0), font=font)
    img.save(out, quality=95)
    return out


@pytest.fixture(scope="module")
def photo_like_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _tesseract():
        pytest.skip("tesseract not available")
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=(450, 800, 3), dtype=np.uint8)
    out = tmp_path_factory.mktemp("ocr") / "photo.jpg"
    Image.fromarray(arr).save(out, quality=85)
    return out


def test_ocr_extracts_title_card_text(title_card_image: Path) -> None:
    result = ocr_title_cards.ocr_one_frame(title_card_image)
    # Tesseract's word-segmentation varies across builds (e.g. Homebrew vs Ubuntu
    # apt with different default fonts) — sometimes spaces drop, sometimes a
    # letter substitutes. Don't pin to exact spelling. Just confirm the OCR
    # produced enough non-trivial text and classified the frame as a title card.
    text_lower = result["text"].lower()
    assert any(token in text_lower for token in ("cartoon", "kang", "disco", "dance"))
    assert result["confidence"] > 50.0
    assert result["is_title_card"] is True


def test_ocr_does_not_classify_noise_as_title_card(photo_like_image: Path) -> None:
    result = ocr_title_cards.ocr_one_frame(photo_like_image)
    # Random noise frame: high bg_std, low/no real text.
    assert result["is_title_card"] is False


def test_classify_title_card_thresholds() -> None:
    # All conditions met
    assert ocr_title_cards.classify_title_card(
        text="STUDIO GHIBLI PRESENTS THE FELLOWSHIP", confidence=85, n_words=5, bg_std=20.0
    )
    # Too few characters
    assert not ocr_title_cards.classify_title_card("hi", 90, 1, 10.0)
    # Low confidence
    assert not ocr_title_cards.classify_title_card("Some readable text here", 30, 4, 10.0)
    # Too noisy a background (real photo with text overlay shouldn't pass)
    assert not ocr_title_cards.classify_title_card("Photo caption here", 80, 3, 80.0)


def test_ocr_for_recipe_writes_per_cut_files(
    title_card_image: Path, photo_like_image: Path, tmp_path: Path
) -> None:
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 2.0, "duration_sec": 2.0, "flagged_short": False},
        {"index": 1, "start_sec": 2.0, "end_sec": 4.0, "duration_sec": 2.0, "flagged_short": False},
    ]
    cuts_json = tmp_path / "cuts.json"
    cuts_json.write_text(json.dumps(cuts))
    cuts_dir = tmp_path / "cuts"
    (cuts_dir / "0").mkdir(parents=True)
    (cuts_dir / "1").mkdir(parents=True)
    # Cut 0 is the title card; cut 1 is the photo.
    shutil.copy(title_card_image, cuts_dir / "0" / "b.jpg")
    shutil.copy(photo_like_image, cuts_dir / "1" / "b.jpg")

    out_paths = ocr_title_cards.ocr_for_recipe(cuts_dir, cuts_json)
    cut0 = json.loads(out_paths[0].read_text())
    cut1 = json.loads(out_paths[1].read_text())
    assert cut0["is_title_card"] is True
    assert cut1["is_title_card"] is False


def test_ocr_for_recipe_handles_missing_keyframe(tmp_path: Path) -> None:
    if not _tesseract():
        pytest.skip("tesseract not available")
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 2.0, "duration_sec": 2.0, "flagged_short": False},
    ]
    cuts_json = tmp_path / "cuts.json"
    cuts_json.write_text(json.dumps(cuts))
    cuts_dir = tmp_path / "cuts"
    # Don't create the keyframe file at all
    out_paths = ocr_title_cards.ocr_for_recipe(cuts_dir, cuts_json)
    data = json.loads(out_paths[0].read_text())
    assert data["is_title_card"] is False
    assert "keyframe not found" in data["error"]
