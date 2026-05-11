"""Tests for scripts.eval_recipe."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import eval_recipe


def test_similarity_of_identical_strings_is_1() -> None:
    s = "A litter of golden retriever puppies playing in the snow"
    assert eval_recipe.similarity(s, s) == 1.0


def test_similarity_of_disjoint_strings_is_0() -> None:
    assert eval_recipe.similarity("apple banana cherry", "xyzzy plover frobnicate") == 0.0


def test_similarity_paraphrase_scores_in_middle() -> None:
    gt = "A litter of golden retriever puppies playing in the snow"
    inferred = "Three golden retriever puppies play in fresh snow at a snowy field"
    score = eval_recipe.similarity(gt, inferred)
    assert 0.2 < score < 0.8, f"expected partial overlap, got {score}"


def test_similarity_handles_empty() -> None:
    assert eval_recipe.similarity("", "anything") == 0.0
    assert eval_recipe.similarity("anything", "") == 0.0
    assert eval_recipe.similarity("", "") == 0.0


def _scaffold_recipe(path: Path, cuts: list[dict]) -> Path:
    recipe = {
        "schema_version": "0.4",
        "source_url": "https://example.com",
        "video_id": "demo",
        "duration_sec": sum(c["duration_sec"] for c in cuts),
        "resolution": "1920x1080",
        "fps": 30,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "cuts": cuts,
        "audio": None,
        "assembly": None,
        "model_attribution": None,
    }
    out = path / "recipe.json"
    out.write_text(json.dumps(recipe))
    return out


def _make_cut(
    index: int,
    *,
    inferred_prompt: str | None = None,
    paired: str | None = None,
    kind: str = "ai_clip",
) -> dict:
    inferred_obj = None
    if inferred_prompt is not None:
        inferred_obj = {
            "subject": "x",
            "action": "x",
            "setting": "x",
            "style": "x",
            "camera": "x",
            "lighting": "x",
            "duration_sec": 2.0,
            "aspect_ratio": "16:9",
            "prompt": inferred_prompt,
        }
    return {
        "index": index,
        "start_sec": index * 2.0,
        "end_sec": (index + 1) * 2.0,
        "duration_sec": 2.0,
        "flagged_short": False,
        "keyframes": [f"cuts/{index}/{n}.jpg" for n in ("a", "b", "c")],
        "inferred_kind": kind,
        "inferred": inferred_obj,
        "inferred_error": None,
        "transcript": None,
        "ocr_text": None,
        "ocr_confidence": None,
        "paired_prompt_text": paired,
    }


def test_evaluate_skips_cuts_without_ground_truth(tmp_path: Path) -> None:
    cuts = [
        _make_cut(0, inferred_prompt="some prompt"),  # no paired -> skipped
        _make_cut(
            1,
            inferred_prompt="A litter of golden retriever puppies playing in the snow",
            paired="A litter of golden retriever puppies playing in the snow",
        ),
    ]
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    out = eval_recipe.evaluate(recipe_path)
    assert out["summary"]["n_evaluable_cuts"] == 1
    assert out["summary"]["mean_similarity"] == 1.0
    assert out["per_cut"][0]["index"] == 1


def test_evaluate_handles_no_evaluable_cuts(tmp_path: Path) -> None:
    cuts = [_make_cut(0, inferred_prompt="prompt")]  # no paired_prompt_text anywhere
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    out = eval_recipe.evaluate(recipe_path)
    assert out["summary"]["n_evaluable_cuts"] == 0
    assert out["summary"]["mean_similarity"] is None
    assert out["per_cut"] == []


def test_evaluate_writes_eval_json_next_to_recipe(tmp_path: Path) -> None:
    cuts = [_make_cut(0, inferred_prompt="hello world", paired="hello world")]
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    eval_recipe.evaluate(recipe_path)
    eval_path = recipe_path.parent / "eval.json"
    assert eval_path.exists()
    on_disk = json.loads(eval_path.read_text())
    assert on_disk["recipe_video_id"] == "demo"
    assert on_disk["per_cut"][0]["similarity"] == 1.0


def test_main_threshold_passes(tmp_path: Path) -> None:
    cuts = [_make_cut(0, inferred_prompt="hello world", paired="hello world")]
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    rc = eval_recipe.main([str(recipe_path), "--min-similarity", "0.5"])
    assert rc == 0


def test_main_threshold_fails(tmp_path: Path) -> None:
    cuts = [_make_cut(0, inferred_prompt="hello world", paired="completely different")]
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    rc = eval_recipe.main([str(recipe_path), "--min-similarity", "0.5"])
    assert rc == 1


def test_main_threshold_no_data(tmp_path: Path) -> None:
    cuts = [_make_cut(0, inferred_prompt="prompt")]  # no paired
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    rc = eval_recipe.main([str(recipe_path), "--min-similarity", "0.5"])
    assert rc == 2


def test_main_no_threshold_returns_zero_even_with_no_cuts(tmp_path: Path) -> None:
    cuts = [_make_cut(0, inferred_prompt="prompt")]
    recipe_path = _scaffold_recipe(tmp_path, cuts)
    rc = eval_recipe.main([str(recipe_path)])
    assert rc == 0


def test_similarity_identical_short_string() -> None:
    """Edge case: a single short token still scores 1.0 against itself."""
    assert eval_recipe.similarity("a", "a") == 1.0


def test_similarity_token_overlap_dominates_when_ngrams_diverge() -> None:
    """Token Jaccard is order-independent; char n-grams are not. The mean
    therefore lands meaningfully above 0 when tokens fully match, even when
    word order is reversed.
    """
    score = eval_recipe.similarity("hello world", "world hello")
    # Token channel == 1.0; n-gram channel < 1.0; mean > 0.5
    assert score > 0.5
