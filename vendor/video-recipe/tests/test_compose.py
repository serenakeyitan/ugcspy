"""Tests for scripts.compose recipe-contract handling.

These tests guard against the recipe-contract drift that shipped in PR
#11 and was caught by audit (issue #12): compose.py reads from a
different recipe shape than assemble_recipe.py writes. Any new schema
revision should add a fixture here so a future regression is impossible.

These are PURE-COMPUTE tests. No Kling, no OpenAI, no real Whisper. The
goal is to assert the prompt-resolver covers every supported shape and
the validator fails loudly on malformed recipes BEFORE any API spend.
"""

from __future__ import annotations

import pytest

from scripts import compose

# ─── resolve_cut_prompt: every supported shape ─────────────────────────────


def test_resolve_v05_nested_prompt():
    """v0.5 canonical shape: cut.inferred.prompt"""
    cut = {
        "index": 0,
        "inferred": {
            "subject": "woman",
            "action": "gestures",
            "prompt": "woman gestures to camera, vertical 9:16",
        },
    }
    assert compose.resolve_cut_prompt(cut) == "woman gestures to camera, vertical 9:16"


def test_resolve_legacy_top_level_prompt():
    """Legacy / hand-edited recipes: cut.inferred_generation_prompt (top level).
    The bundled 7630138325545880845/recipe.json uses this shape."""
    cut = {
        "index": 0,
        "inferred_generation_prompt": "medium close-up of a person seated at a desk",
    }
    assert (
        compose.resolve_cut_prompt(cut)
        == "medium close-up of a person seated at a desk"
    )


def test_resolve_pre_v04_scene_description():
    """Pre-v0.4 shape: cut.scene_description (still supported)"""
    cut = {"index": 0, "scene_description": "static shot of a notebook on a desk"}
    assert compose.resolve_cut_prompt(cut) == "static shot of a notebook on a desk"


def test_resolve_v05_takes_priority_over_legacy():
    """A recipe with BOTH shapes (e.g. mid-migration) should prefer v0.5."""
    cut = {
        "inferred": {"prompt": "v0.5 prompt"},
        "inferred_generation_prompt": "legacy prompt",
        "scene_description": "ancient prompt",
    }
    assert compose.resolve_cut_prompt(cut) == "v0.5 prompt"


def test_resolve_legacy_takes_priority_over_pre_v04():
    """When v0.5 is missing, prefer legacy over scene_description."""
    cut = {
        "inferred_generation_prompt": "legacy prompt",
        "scene_description": "ancient prompt",
    }
    assert compose.resolve_cut_prompt(cut) == "legacy prompt"


def test_resolve_empty_string_is_treated_as_missing():
    """Empty-string prompts should not satisfy the resolver — they'd
    produce a bad render. Skip them and fall through to the next shape."""
    cut = {
        "inferred": {"prompt": "   "},
        "inferred_generation_prompt": "real prompt",
    }
    assert compose.resolve_cut_prompt(cut) == "real prompt"


def test_resolve_returns_none_when_no_prompt():
    cut = {"index": 0}
    assert compose.resolve_cut_prompt(cut) is None


def test_resolve_returns_none_when_inferred_is_null():
    """assemble_recipe writes `inferred: null` for non-AI cuts."""
    cut = {"index": 0, "inferred": None}
    assert compose.resolve_cut_prompt(cut) is None


# ─── resolve_recipe_full_transcript ────────────────────────────────────────


def test_resolve_transcript_v05_tts_script():
    """v0.5 canonical shape: top-level tts.script"""
    recipe = {"tts": {"script": "Welcome to my video.", "language": "en"}}
    assert compose.resolve_recipe_full_transcript(recipe) == "Welcome to my video."


def test_resolve_transcript_legacy_voiceover():
    """Legacy shape: voiceover.full_transcript"""
    recipe = {"voiceover": {"full_transcript": "legacy script"}}
    assert compose.resolve_recipe_full_transcript(recipe) == "legacy script"


def test_resolve_transcript_v05_takes_priority():
    """Both shapes present → v0.5 wins."""
    recipe = {
        "tts": {"script": "v0.5 script"},
        "voiceover": {"full_transcript": "legacy script"},
    }
    assert compose.resolve_recipe_full_transcript(recipe) == "v0.5 script"


def test_resolve_transcript_empty_when_no_shape():
    assert compose.resolve_recipe_full_transcript({}) == ""
    # tts present but script is empty → fall through to legacy
    assert (
        compose.resolve_recipe_full_transcript(
            {"tts": {"script": ""}, "voiceover": {"full_transcript": "legacy"}}
        )
        == "legacy"
    )


# ─── validate_compose_ready ────────────────────────────────────────────────


def test_validate_passes_when_all_cuts_have_prompts():
    cuts = [
        {"index": 0, "inferred": {"prompt": "first"}},
        {"index": 1, "inferred_generation_prompt": "second"},
        {"index": 2, "scene_description": "third"},
    ]
    # Should not raise
    compose.validate_compose_ready(cuts)


def test_validate_fails_with_cut_index_when_prompt_missing(capsys):
    cuts = [
        {"index": 0, "inferred": {"prompt": "first"}},
        {"index": 1},  # missing
        {"index": 2, "inferred_generation_prompt": "third"},
    ]
    with pytest.raises(SystemExit) as exc:
        compose.validate_compose_ready(cuts)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    # The error message must name cut 1 specifically
    assert "[1]" in captured.err or "cut(s) [1]" in captured.err


def test_validate_fails_with_multiple_missing_cuts(capsys):
    cuts = [{"index": 0}, {"index": 1, "inferred": {"prompt": "ok"}}, {"index": 2}]
    with pytest.raises(SystemExit) as exc:
        compose.validate_compose_ready(cuts)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "0" in captured.err and "2" in captured.err


def test_validate_fails_when_inferred_is_null_and_no_fallback(capsys):
    """assemble_recipe writes inferred: null for non-AI cuts. compose
    needs to either refuse those cuts or have a fallback. Currently
    we refuse — non-AI cuts are caught earlier by the N/A prefix
    check, but if they aren't, validation catches it."""
    cuts = [{"index": 0, "inferred": None, "inferred_kind": "non_ai_footage"}]
    with pytest.raises(SystemExit) as exc:
        compose.validate_compose_ready(cuts)
    assert exc.value.code == 1
