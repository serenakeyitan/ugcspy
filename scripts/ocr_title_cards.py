"""OCR keyframes to extract on-screen text and detect title cards.

For each cut, runs tesseract on the middle keyframe and writes
``recipes/<id>/cuts/<i>/ocr.json``:

    {"text": "...", "confidence": 0.92, "is_title_card": true}

A frame is classified as a title card by the heuristic in
``classify_title_card``: high text density + low non-text-pixel diversity
(uniform background) + reasonable OCR confidence.

Tesseract is required; install via ``brew install tesseract`` (macOS) or
``apt-get install tesseract-ocr`` (Debian/Ubuntu).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# A frame is title-card-like when:
# - OCR finds at least this many non-whitespace characters
MIN_TEXT_CHARS = 12
# - Average per-word OCR confidence is at least this
MIN_OCR_CONFIDENCE = 60.0
# - Background pixel-color variance is below this (uniform / mostly-white-or-black)
MAX_BG_STD = 35.0


def _tesseract_bin() -> str:
    bin_path = shutil.which("tesseract")
    if not bin_path:
        raise RuntimeError(
            "tesseract not found on PATH — install via `brew install tesseract`"
            " or `apt-get install tesseract-ocr`"
        )
    return bin_path


def _run_tesseract_tsv(image_path: Path) -> str:
    """Return raw TSV output from tesseract (one row per word with confidence).

    Runs from the image's parent directory with a relative filename — some
    leptonica builds (notably the Homebrew tesseract on macOS) fail on certain
    absolute paths but handle the same file via a relative reference.

    Decodes stdout tolerantly — tesseract sometimes emits non-UTF-8 bytes when
    OCR confidence is very low (it tries to transcribe noise as glyphs).
    """
    image_path = image_path.resolve()
    cmd = [_tesseract_bin(), image_path.name, "stdout", "-l", "eng", "--psm", "3", "tsv"]
    result = subprocess.run(
        cmd,
        cwd=str(image_path.parent),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _parse_tsv(tsv: str) -> tuple[str, float, int]:
    """Parse tesseract TSV. Returns (joined_text, mean_confidence, n_words)."""
    lines = tsv.strip().splitlines()
    if not lines:
        return "", 0.0, 0
    header = lines[0].split("\t")
    try:
        conf_idx = header.index("conf")
        text_idx = header.index("text")
    except ValueError:
        return "", 0.0, 0

    words: list[str] = []
    confs: list[float] = []
    for row in lines[1:]:
        cols = row.split("\t")
        if len(cols) <= max(conf_idx, text_idx):
            continue
        text = cols[text_idx].strip()
        if not text:
            continue
        try:
            conf = float(cols[conf_idx])
        except ValueError:
            continue
        if conf < 0:
            # tesseract emits -1 for non-word rows (spans, paragraphs)
            continue
        words.append(text)
        confs.append(conf)
    if not words:
        return "", 0.0, 0
    return " ".join(words), sum(confs) / len(confs), len(words)


def _bg_std(image_path: Path) -> float:
    """Approximate background uniformity: standard deviation of pixel values
    around the frame edges. Low std => uniform background (title-card-like).
    Returns a stand-in value when PIL/numpy aren't usable.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return float("nan")

    img = Image.open(image_path).convert("L")
    arr = np.asarray(img)
    h, w = arr.shape
    border = max(1, min(h, w) // 20)
    edges = np.concatenate(
        [
            arr[:border, :].ravel(),
            arr[-border:, :].ravel(),
            arr[:, :border].ravel(),
            arr[:, -border:].ravel(),
        ]
    )
    return float(edges.std())


def classify_title_card(text: str, confidence: float, n_words: int, bg_std: float) -> bool:
    """Return True iff this frame looks like a pure-text title card."""
    if len(text.strip()) < MIN_TEXT_CHARS:
        return False
    if n_words < 2:
        return False
    if confidence < MIN_OCR_CONFIDENCE:
        return False
    # NaN-safe: NaN comparisons return False, so a NaN bg_std bypasses this check.
    return not (bg_std == bg_std and bg_std > MAX_BG_STD)


def ocr_one_frame(image_path: Path) -> dict[str, Any]:
    """Run tesseract on one image and return a structured result."""
    tsv = _run_tesseract_tsv(image_path)
    text, confidence, n_words = _parse_tsv(tsv)
    bg = _bg_std(image_path)
    return {
        "text": text,
        "confidence": round(confidence, 2),
        "n_words": n_words,
        "bg_std": None if bg != bg else round(bg, 2),  # NaN-safe
        "is_title_card": classify_title_card(text, confidence, n_words, bg),
    }


def ocr_for_recipe(
    cuts_dir: Path,
    cuts_json: Path,
    *,
    keyframe: str = "b",
) -> dict[int, Path]:
    """For each cut, OCR the chosen keyframe (default 'b', the midpoint frame)
    and write <cuts_dir>/<index>/ocr.json. Returns map of index -> path.
    """
    cuts: list[dict[str, Any]] = json.loads(cuts_json.read_text())
    results: dict[int, Path] = {}
    for cut in cuts:
        index = int(cut["index"])
        cut_dir = cuts_dir / str(index)
        cut_dir.mkdir(parents=True, exist_ok=True)
        frame_path = cut_dir / f"{keyframe}.jpg"
        out_path = cut_dir / "ocr.json"
        if not frame_path.exists():
            out_path.write_text(
                json.dumps(
                    {
                        "text": "",
                        "confidence": 0.0,
                        "n_words": 0,
                        "bg_std": None,
                        "is_title_card": False,
                        "error": f"keyframe not found: {frame_path.name}",
                    },
                    indent=2,
                )
            )
            results[index] = out_path
            continue
        ocr_result = ocr_one_frame(frame_path)
        out_path.write_text(json.dumps(ocr_result, indent=2))
        results[index] = out_path
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="OCR each cut's middle keyframe.")
    parser.add_argument("cuts_dir", type=Path, help="Directory of <index>/{a,b,c}.jpg")
    parser.add_argument("cuts_json", type=Path, help="cuts.json from detect_cuts")
    parser.add_argument(
        "--keyframe",
        choices=("a", "b", "c"),
        default="b",
        help="Which keyframe to OCR (default 'b', the midpoint).",
    )
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("ocr_title_cards"):
        results = ocr_for_recipe(args.cuts_dir, args.cuts_json, keyframe=args.keyframe)
    title_count = sum(1 for p in results.values() if json.loads(p.read_text()).get("is_title_card"))
    print(f"OCR complete: {len(results)} cuts, {title_count} classified as title_card")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
