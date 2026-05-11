"""Assemble final recipe.json from cuts.json + per-cut inferred.json files.

Reads:
  <recipe_dir>/source.info.json   (from scripts.download)
  <recipe_dir>/cuts.json          (from scripts.detect_cuts)
  <recipe_dir>/cuts/<i>/inferred.json   (written by the agent in stage 4)
  <recipe_dir>/cuts/<i>/{a,b,c}.jpg     (from scripts.extract_keyframes)

Writes:
  <recipe_dir>/recipe.json

Validates the output against schemas/recipe.v0.2.json.

Two shapes for inferred.json — both written by the agent in stage 4:

  ai_clip  ->  {"subject": ..., "action": ..., ..., "prompt": ...}
  null kind -> {"inferred_kind": "title_card" | "non_ai_footage" |
                                  "lumped_cuts" | "transition" | "unreadable",
                "error": "..."}

The assembler infers `inferred_kind: "ai_clip"` when the file has the full
prompt object and no explicit kind.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.5"

NULL_KINDS = frozenset({"title_card", "non_ai_footage", "lumped_cuts", "transition", "unreadable"})


def _load_inferred(
    cut_dir: Path,
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Return (inferred_obj_or_None, error_or_None, inferred_kind)."""
    path = cut_dir / "inferred.json"
    if not path.exists():
        return None, "inferred.json not found", "unreadable"

    data = json.loads(path.read_text())

    explicit_kind = data.get("inferred_kind")
    if explicit_kind is not None:
        if explicit_kind == "ai_clip":
            # The file should be the full prompt object; allow inferred_kind +
            # the rest, strip the kind before embedding.
            obj = {k: v for k, v in data.items() if k != "inferred_kind"}
            return obj, None, "ai_clip"
        if explicit_kind in NULL_KINDS:
            return None, str(data.get("error", "no error message provided")), explicit_kind
        raise ValueError(f"unknown inferred_kind in {path}: {explicit_kind!r}")

    # Legacy / implicit shapes from before #19.
    if "inferred" in data and data["inferred"] is None:
        return None, str(data.get("error", "unknown error")), "unreadable"

    # Otherwise the file is the full prompt object — treat as ai_clip.
    return data, None, "ai_clip"


def _load_schema(repo_root: Path) -> dict[str, Any]:
    schema_path = repo_root / "schemas" / f"recipe.v{SCHEMA_VERSION}.json"
    return json.loads(schema_path.read_text())


def _validate(recipe: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate recipe against schema. jsonschema is optional."""
    try:
        import jsonschema
    except ImportError:
        for field in schema["required"]:
            if field not in recipe:
                raise ValueError(f"recipe missing required field: {field}") from None
        return
    jsonschema.validate(recipe, schema)


def assemble(
    source_url: str,
    recipe_dir: Path,
    repo_root: Path,
    *,
    now: dt.datetime | None = None,
) -> Path:
    """Build recipe.json from artifacts in recipe_dir. Returns the output path."""
    info_path = recipe_dir / "source.info.json"
    cuts_path = recipe_dir / "cuts.json"
    if not cuts_path.exists():
        raise FileNotFoundError(f"cuts.json not found at {cuts_path}")

    info: dict[str, Any] = json.loads(info_path.read_text()) if info_path.exists() else {}
    cuts_in: list[dict[str, Any]] = json.loads(cuts_path.read_text())

    cuts_out: list[dict[str, Any]] = []
    for cut in cuts_in:
        index = int(cut["index"])
        cut_dir = recipe_dir / "cuts" / str(index)
        keyframes = [f"cuts/{index}/{n}.jpg" for n in ("a", "b", "c")]
        for kf in keyframes:
            if not (recipe_dir / kf).exists():
                raise FileNotFoundError(f"missing keyframe: {recipe_dir / kf}")
        inferred, error, kind = _load_inferred(cut_dir)
        # Lift `caption` from the inferred object up to the cut level so it sits
        # alongside transcript, ocr_text, etc. per schema v0.5. Default null.
        caption: str | None = None
        if isinstance(inferred, dict) and "caption" in inferred:
            raw = inferred.pop("caption")
            if isinstance(raw, str) and raw.strip():
                caption = raw.strip()
        # Per-cut transcript (optional — only present when transcribe.py ran).
        transcript_path = cut_dir / "transcript.json"
        transcript_text: str | None = None
        if transcript_path.exists():
            transcript_data = json.loads(transcript_path.read_text())
            transcript_text = (transcript_data.get("text") or "").strip() or None
        # Per-cut OCR (optional — only present when ocr_title_cards.py ran).
        ocr_path = cut_dir / "ocr.json"
        ocr_text: str | None = None
        ocr_confidence: float | None = None
        if ocr_path.exists():
            ocr_data = json.loads(ocr_path.read_text())
            text = (ocr_data.get("text") or "").strip()
            ocr_text = text or None
            conf_val = ocr_data.get("confidence")
            ocr_confidence = float(conf_val) if conf_val is not None else None
        cuts_out.append(
            {
                "index": index,
                "start_sec": cut["start_sec"],
                "end_sec": cut["end_sec"],
                "duration_sec": cut["duration_sec"],
                "flagged_short": cut.get("flagged_short", False),
                "keyframes": keyframes,
                "inferred_kind": kind,
                "inferred": inferred,
                "inferred_error": error,
                "transcript": transcript_text,
                "ocr_text": ocr_text,
                "ocr_confidence": ocr_confidence,
                "paired_prompt_text": None,  # filled in below
                "caption": caption,
            }
        )

    # Title-card pairing: for any cut classified as title_card with non-empty
    # ocr_text, attach the ocr_text as paired_prompt_text on the immediately
    # following cut. Captures announcement-reel structure (text card → clip).
    for i in range(len(cuts_out) - 1):
        cur = cuts_out[i]
        nxt = cuts_out[i + 1]
        if cur["inferred_kind"] == "title_card" and cur["ocr_text"]:
            nxt["paired_prompt_text"] = cur["ocr_text"]

    duration_sec = (
        info.get("duration")
        if info.get("duration") is not None
        else (cuts_out[-1]["end_sec"] - cuts_out[0]["start_sec"] if cuts_out else 0.0)
    )

    width = info.get("width")
    height = info.get("height")
    resolution = f"{width}x{height}" if width and height else None

    video_id = info.get("id") or recipe_dir.name

    # Audio block — populated when transcript.json exists at recipe root.
    audio_block: dict[str, Any] | None = None
    transcript_root = recipe_dir / "transcript.json"
    if transcript_root.exists():
        transcript_doc = json.loads(transcript_root.read_text())
        audio_block = {
            "language": transcript_doc.get("language"),
            "transcript_path": "transcript.json",
            "duration_sec": float(transcript_doc.get("duration_sec", 0.0)),
        }

    # Hook block — populated when hook.json exists at recipe root (the agent
    # writes it after stage 4 per prompts/identify_hook.md).
    hook_block: dict[str, Any] | None = None
    hook_path = recipe_dir / "hook.json"
    if hook_path.exists():
        hook_data = json.loads(hook_path.read_text())
        # Two valid shapes: {"hook": null} OR the full hook object directly.
        if "hook" in hook_data and hook_data["hook"] is None:
            hook_block = None
        elif "pattern" in hook_data:
            hook_block = hook_data
        # Otherwise leave hook_block as None — file is malformed; better to be
        # silent than to fabricate.

    # TTS block — populated when tts.json exists (the agent writes it after
    # transcription per prompts/identify_tts.md). Required fields:
    # script, language, duration_sec, likely_synthetic.
    tts_block: dict[str, Any] | None = None
    tts_path = recipe_dir / "tts.json"
    if tts_path.exists():
        tts_data = json.loads(tts_path.read_text())
        # Two valid shapes: {"tts": null} OR a full tts object. Anything else
        # is treated as a tts object — missing fields will surface downstream
        # when jsonschema validates the assembled recipe.
        is_explicit_null = "tts" in tts_data and tts_data["tts"] is None
        tts_block = None if is_explicit_null else tts_data

    now = now or dt.datetime.now(dt.UTC)
    recipe = {
        "schema_version": SCHEMA_VERSION,
        "source_url": source_url,
        "video_id": video_id,
        "duration_sec": float(duration_sec),
        "resolution": resolution,
        "fps": info.get("fps"),
        "generated_at": now.isoformat(timespec="seconds"),
        "hook": hook_block,
        "cuts": cuts_out,
        "tts": tts_block,
        "audio": audio_block,
        "assembly": None,
        "model_attribution": None,
    }

    schema = _load_schema(repo_root)
    _validate(recipe, schema)

    out_path = recipe_dir / "recipe.json"
    out_path.write_text(json.dumps(recipe, indent=2, sort_keys=False))
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Assemble final recipe.json.")
    parser.add_argument("source_url")
    parser.add_argument("recipe_dir", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repo root (where schemas/ lives). Default: parent of scripts/.",
    )
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("assemble_recipe"):
        out = assemble(args.source_url, args.recipe_dir, args.repo_root)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
