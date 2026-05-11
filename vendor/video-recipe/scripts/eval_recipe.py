"""Reproducibility eval: score how close inferred prompts are to ground-truth.

For each cut where ``paired_prompt_text`` (from OCR'd title card, #15) and
``inferred.prompt`` (from the agent's stage 4) are both present, compute a
similarity score and aggregate.

v1 uses a deterministic three-channel similarity:

  - **Token Jaccard** on lowercased word sets — symmetric content overlap.
  - **Char n-gram Jaccard** on 3-grams — robust to small phrasing variations.
  - **Token recall over ground truth** — what fraction of ground-truth tokens
    appear in the inferred prompt. Critical because the agent's inferred
    prompt is typically more verbose than a short OCR'd seed prompt; symmetric
    Jaccard penalizes that even when the inferred text fully covers the GT
    semantics.

Final ``similarity`` is the mean of the three. Produces a number in [0, 1].

Future work (phase 3): swap in semantic embeddings (sentence-transformers
MiniLM) for a stronger meaning-based score. The three-channel approach gets
us a useful signal without a 200MB model download.

Output: ``recipes/<id>/eval.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

EVAL_SCHEMA_VERSION = "0.1"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    text = re.sub(r"\s+", " ", text.lower().strip())
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _recall(reference: set[str], hypothesis: set[str]) -> float:
    """Fraction of reference tokens that appear in hypothesis."""
    if not reference:
        return 0.0
    return len(reference & hypothesis) / len(reference)


def similarity(ground_truth: str, inferred: str) -> float:
    """Three-channel similarity in [0, 1]: token Jaccard + char n-gram Jaccard
    + token recall over ground truth.
    """
    gt_tokens = _tokens(ground_truth)
    inf_tokens = _tokens(inferred)
    tok_jaccard = _jaccard(gt_tokens, inf_tokens)
    ngram_score = _jaccard(_char_ngrams(ground_truth), _char_ngrams(inferred))
    tok_recall = _recall(gt_tokens, inf_tokens)
    return round((tok_jaccard + ngram_score + tok_recall) / 3.0, 4)


def evaluate(recipe_path: Path) -> dict[str, Any]:
    """Score every evaluable cut and write eval.json next to recipe.json."""
    recipe = json.loads(recipe_path.read_text())
    per_cut: list[dict[str, Any]] = []
    for cut in recipe.get("cuts", []):
        gt = cut.get("paired_prompt_text")
        inferred = cut.get("inferred")
        inferred_prompt = inferred.get("prompt") if isinstance(inferred, dict) else None
        if not gt or not inferred_prompt:
            continue
        score = similarity(gt, inferred_prompt)
        per_cut.append(
            {
                "index": int(cut["index"]),
                "ground_truth": gt,
                "inferred": inferred_prompt,
                "similarity": score,
            }
        )

    if per_cut:
        scores = [c["similarity"] for c in per_cut]
        summary = {
            "n_evaluable_cuts": len(per_cut),
            "mean_similarity": round(sum(scores) / len(scores), 4),
            "min_similarity": min(scores),
            "max_similarity": max(scores),
        }
    else:
        summary = {
            "n_evaluable_cuts": 0,
            "mean_similarity": None,
            "min_similarity": None,
            "max_similarity": None,
        }

    out = {
        "schema_version": EVAL_SCHEMA_VERSION,
        "recipe_video_id": recipe.get("video_id"),
        "summary": summary,
        "per_cut": per_cut,
    }
    out_path = recipe_path.parent / "eval.json"
    out_path.write_text(json.dumps(out, indent=2))
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Score recipe vs ground-truth prompts.")
    parser.add_argument("recipe_path", type=Path, help="recipes/<id>/recipe.json")
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="If set, exit non-zero when mean similarity falls below this value.",
    )
    args = parser.parse_args(argv)
    from scripts._log import stage

    with stage("eval_recipe"):
        out = evaluate(args.recipe_path)
    summary = out["summary"]
    print(
        f"eval: {summary['n_evaluable_cuts']} cuts evaluable, "
        f"mean similarity {summary['mean_similarity']}"
    )
    if args.min_similarity is not None:
        mean = summary["mean_similarity"]
        if mean is None:
            print("no evaluable cuts; cannot check threshold", file=sys.stderr)
            return 2
        if mean < args.min_similarity:
            print(
                f"FAIL: mean {mean} < threshold {args.min_similarity}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
