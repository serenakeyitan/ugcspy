"""Heuristic model attribution: guess which video model produced each AI clip.

Sources of evidence (in roughly decreasing order of confidence):

1. **Textual mentions** in source title / description / transcript / OCR text.
   When a creator says "made with Veo 3", that's near-certain.
2. **Watermarks** in keyframe corners. Sora watermarks bottom-right; Runway
   has a small mark; Kling sometimes shows in the lower edge. Heuristic only.
3. **Visual fingerprints** — placeholder hooks for phase 3 (motion artifacts,
   color signature). Not implemented in v1.

Output: a ``model_attribution`` block written into ``recipe.json`` after
assembly. This script reads the existing recipe.json, augments it, and
re-writes it. (Called as a post-pass rather than during initial assembly so
re-runs don't require re-running the whole pipeline.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Known models. Add patterns as we encounter them in the wild.
KNOWN_MODELS: dict[str, dict[str, Any]] = {
    "sora": {
        "names": ["sora", "openai sora"],
        "name_patterns": [r"\bsora\b(?:\s*\d+)?", r"\bopenai\s+sora\b"],
        "watermark_region": "bottom-right",
    },
    "veo": {
        "names": ["veo", "veo 2", "veo 3", "google veo"],
        "name_patterns": [r"\bveo\s*\d?\b", r"\bgoogle\s+veo\b"],
        "watermark_region": "bottom-right",
    },
    "runway": {
        "names": ["runway", "runway gen-2", "runway gen-3", "runway gen-4"],
        "name_patterns": [r"\brunway(?:\s+gen-?[234])?\b", r"\bgen-?[234]\b"],
        "watermark_region": "bottom-right",
    },
    "kling": {
        "names": ["kling"],
        "name_patterns": [r"\bkling\s*\d?\b"],
        "watermark_region": "bottom-edge",
    },
    "pika": {
        "names": ["pika", "pika labs"],
        "name_patterns": [r"\bpika(?:\s+labs)?\b"],
        "watermark_region": "bottom-right",
    },
    "luma": {
        "names": ["luma", "luma dream machine", "dream machine"],
        "name_patterns": [r"\bluma\b", r"\bdream\s+machine\b"],
        "watermark_region": "bottom-right",
    },
}


def _gather_text_corpus(recipe: dict[str, Any], info: dict[str, Any]) -> str:
    """Pull every piece of text we have for this video into one searchable blob."""
    parts: list[str] = []
    if info:
        for k in ("title", "description", "uploader", "uploader_id"):
            v = info.get(k)
            if v:
                parts.append(str(v))
    for cut in recipe.get("cuts", []):
        for k in ("transcript", "ocr_text", "paired_prompt_text"):
            v = cut.get(k)
            if v:
                parts.append(str(v))
        inferred = cut.get("inferred")
        if isinstance(inferred, dict):
            for k in ("style", "prompt"):
                v = inferred.get(k)
                if v:
                    parts.append(str(v))
    return " | ".join(parts)


def find_textual_mentions(text: str) -> dict[str, list[str]]:
    """Return {model_id: [matched substrings]} for every model named in ``text``."""
    text_lower = text.lower()
    hits: dict[str, list[str]] = {}
    for model_id, spec in KNOWN_MODELS.items():
        matches: list[str] = []
        for pat in spec["name_patterns"]:
            matches.extend(re.findall(pat, text_lower))
        if matches:
            # De-dup while preserving order.
            seen: set[str] = set()
            ordered: list[str] = []
            for m in matches:
                if m not in seen:
                    seen.add(m)
                    ordered.append(m)
            hits[model_id] = ordered
    return hits


def _watermark_region(image_path: Path, region: str) -> dict[str, Any] | None:
    """Quick low-cost watermark probe: look at the named corner/edge for
    consistent dark-or-light patches against the surrounding pixels.

    Returns ``{"strength": float, "region": str}`` or None when PIL is missing.

    This is a placeholder heuristic. Real watermark detection would require
    per-model templates; we just record that something watermark-shaped is
    present for now.
    """
    try:
        import numpy as np
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return None

    try:
        img = Image.open(image_path).convert("L")
    except (OSError, UnidentifiedImageError):
        # Corrupt or non-image file — no watermark signal.
        return None
    arr = np.asarray(img, dtype=float)
    h, w = arr.shape
    box_h = max(1, h // 12)
    box_w = max(1, w // 6)

    if region == "bottom-right":
        patch = arr[h - box_h :, w - box_w :]
    elif region == "bottom-edge":
        patch = arr[h - box_h :, w // 3 : 2 * w // 3]
    else:
        return None

    # Heuristic: a watermark stands out from its local background.
    surrounding = arr[h - 2 * box_h : h - box_h, w - 2 * box_w : w - box_w]
    if surrounding.size == 0:
        return None
    mean_diff = abs(float(patch.mean()) - float(surrounding.mean()))
    # Normalize to 0-1; arbitrary scale, just a weak signal for v1.
    strength = min(mean_diff / 60.0, 1.0)
    return {"strength": round(strength, 3), "region": region}


def detect_watermarks(
    recipe_dir: Path, recipe: dict[str, Any], min_strength: float = 0.45
) -> dict[int, dict[str, Any]]:
    """For each ai_clip cut, probe each known model's watermark region against
    keyframe 'b'. Return ``{cut_index: {model_id: {strength, region}}}``.
    """
    out: dict[int, dict[str, Any]] = {}
    for cut in recipe.get("cuts", []):
        if cut.get("inferred_kind") != "ai_clip":
            continue
        index = int(cut["index"])
        # Use the keyframe relative path that was written in cuts:
        # "cuts/<i>/b.jpg". Resolve relative to recipe_dir.
        keyframes = cut.get("keyframes") or []
        b_frame = next((kf for kf in keyframes if kf.endswith("b.jpg")), None)
        if not b_frame:
            continue
        frame_path = recipe_dir / b_frame
        if not frame_path.exists():
            continue
        per_model: dict[str, Any] = {}
        for model_id, spec in KNOWN_MODELS.items():
            probe = _watermark_region(frame_path, spec["watermark_region"])
            if probe and probe["strength"] >= min_strength:
                per_model[model_id] = probe
        if per_model:
            out[index] = per_model
    return out


def attribute(recipe_dir: Path) -> dict[str, Any]:
    """Read recipe.json + source.info.json, populate model_attribution, write back.

    Returns the new model_attribution block.
    """
    recipe_path = recipe_dir / "recipe.json"
    if not recipe_path.exists():
        raise FileNotFoundError(f"recipe.json not found at {recipe_path}")
    info_path = recipe_dir / "source.info.json"
    info: dict[str, Any] = json.loads(info_path.read_text()) if info_path.exists() else {}

    recipe: dict[str, Any] = json.loads(recipe_path.read_text())

    # 1. Textual mentions (whole-corpus).
    corpus = _gather_text_corpus(recipe, info)
    text_hits = find_textual_mentions(corpus)

    # 2. Watermarks (per-cut).
    watermark_hits = detect_watermarks(recipe_dir, recipe)

    # Score each candidate model.
    # Start with text hits worth 0.7 each (named => high confidence).
    scores: dict[str, float] = {}
    evidence: list[str] = []
    for model_id, matches in text_hits.items():
        scores[model_id] = scores.get(model_id, 0.0) + 0.7
        evidence.append(f"text mentions of {model_id}: {', '.join(matches)}")
    # Watermark hits worth 0.3 each (heuristic, weak).
    for index, per_model in watermark_hits.items():
        for model_id in per_model:
            scores[model_id] = scores.get(model_id, 0.0) + 0.3
            evidence.append(f"watermark-shaped patch for {model_id} on cut {index}")

    # Pick primary model.
    primary_model: str | None = None
    confidence = 0.0
    if scores:
        primary_model, confidence = max(scores.items(), key=lambda kv: kv[1])
        confidence = min(confidence, 1.0)

    # Per-cut: copy the global primary onto cuts that had a per-cut watermark hit
    # for that same model.
    per_cut: dict[str, Any] = {}
    for index, per_model in watermark_hits.items():
        if primary_model and primary_model in per_model:
            per_cut[str(index)] = {
                "model": primary_model,
                "confidence": round(per_model[primary_model]["strength"], 3),
                "evidence": [f"watermark in {per_model[primary_model]['region']}"],
            }

    block: dict[str, Any] = {
        "primary_model": primary_model,
        "confidence": round(confidence, 3) if primary_model else 0.0,
        "evidence": evidence,
        "per_cut": per_cut,
        "candidates": {m: round(min(s, 1.0), 3) for m, s in scores.items()},
    }

    recipe["model_attribution"] = block
    recipe_path.write_text(json.dumps(recipe, indent=2))
    return block


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Heuristic model attribution.")
    parser.add_argument("recipe_dir", type=Path)
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("attribute_model"):
        block = attribute(args.recipe_dir)
    print(
        f"primary_model: {block['primary_model']} "
        f"(confidence {block['confidence']}, "
        f"{len(block['per_cut'])} per-cut hits)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
