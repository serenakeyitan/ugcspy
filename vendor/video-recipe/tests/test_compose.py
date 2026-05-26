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


# ─── kling_billed_duration ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requested,expected",
    [
        (1.0, 5),  # 1s → 5s (rounded up to nearest supported)
        (2.3, 5),  # 2.3s → 5s
        (5.0, 5),  # exactly 5s
        (5.0001, 10),  # just over 5s → 10s
        (6.0, 10),  # 6s → 10s (the case that under-priced the dry-run)
        (9.9, 10),  # 9.9s → 10s
        (10.0, 10),  # exactly 10s
    ],
)
def test_kling_billed_duration_rounding(requested, expected):
    assert compose.kling_billed_duration(requested) == expected


def test_kling_billed_duration_zero():
    """Edge case: a recipe with duration_sec=0 should round to 5 (min)."""
    assert compose.kling_billed_duration(0) == 5


# ─── validate_durations ────────────────────────────────────────────────────


def test_validate_durations_passes_for_in_range_cuts():
    cuts = [
        {"index": 0, "duration_sec": 5.0},
        {"index": 1, "duration_sec": 8.5},
        {"index": 2, "duration_sec": 10.0},
    ]
    # Should not raise
    compose.validate_durations(cuts)


def test_validate_durations_refuses_oversized_cuts(capsys):
    cuts = [
        {"index": 0, "duration_sec": 5.0},
        {"index": 1, "duration_sec": 14.0},  # too long
        {"index": 2, "duration_sec": 22.0},  # too long
    ]
    with pytest.raises(SystemExit) as exc:
        compose.validate_durations(cuts)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    # Names the offending cuts
    assert "cut 1" in err and "14.0" in err
    assert "cut 2" in err and "22.0" in err
    # Doesn't name the in-range cut
    assert "cut 0" not in err
    # Points the user at a remediation
    assert "smaller --max-cut-duration" in err or "split long cuts" in err


def test_validate_durations_passes_with_no_duration_field():
    """A cut missing duration_sec entirely is presumed in-range (0 ≤ 10);
    the prompt validator catches the underlying problem upstream."""
    cuts = [{"index": 0, "inferred": {"prompt": "x"}}]
    compose.validate_durations(cuts)


def test_kling_billed_matches_kling_ts_rounding():
    """Belt-and-suspenders: the Python composer's rounding must match
    src/render/kling.ts:52 (`duration_sec <= 5 ? 5 : 10`). If you change
    one, change both. This test exists so a future drift breaks the
    build instead of the wire-format."""
    # Same boundary conditions as kling.ts
    assert compose.kling_billed_duration(5) == 5
    assert compose.kling_billed_duration(5.0001) == 10
    # The TS code uses `<=`, so a value of exactly 5.0 → 5.
    # Floating-point: very-close-to-5 floats should still round to 5 if
    # they're at or below 5.0.
    assert compose.kling_billed_duration(4.999999) == 5


# ─── decode.json signal loading + gating (issue #14) ───────────────────────


def test_load_decode_signals_returns_none_when_missing(tmp_path):
    """decode.json is optional. Missing → None, no error."""
    assert compose.load_decode_signals(tmp_path) is None


def test_load_decode_signals_returns_parsed_dict(tmp_path):
    (tmp_path / "decode.json").write_text(
        '{"format": {"kind": "talking_head_floating_card", "is_ai_generated": false}}'
    )
    d = compose.load_decode_signals(tmp_path)
    assert d is not None
    assert d["format"]["kind"] == "talking_head_floating_card"


def test_load_decode_signals_handles_invalid_json(tmp_path, capsys):
    (tmp_path / "decode.json").write_text("not valid json")
    result = compose.load_decode_signals(tmp_path)
    assert result is None
    # Should warn but not crash
    captured = capsys.readouterr()
    assert "invalid JSON" in captured.err


# reject_non_ai_recipes


def test_reject_non_ai_recipes_passes_when_no_decode():
    """No decode signal → no opinion → proceed (legacy N/A check still fires)."""
    compose.reject_non_ai_recipes(None)  # should not raise


def test_reject_non_ai_recipes_passes_when_ai_generated():
    decode = {"format": {"kind": "ai_montage_kinetic", "is_ai_generated": True}}
    compose.reject_non_ai_recipes(decode)


def test_reject_non_ai_recipes_fails_when_explicitly_human_shot(capsys):
    decode = {"format": {"kind": "talking_head_with_static_overlay", "is_ai_generated": False}}
    with pytest.raises(SystemExit) as exc:
        compose.reject_non_ai_recipes(decode)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    # Names the format kind so the user can decide whether to override
    assert "talking_head_with_static_overlay" in err
    assert "/ugcspy-fork" in err  # points at the right alternative


def test_reject_non_ai_recipes_no_opinion_when_field_missing():
    """is_ai_generated absent ≠ False. Decode couldn't tell. Let user proceed."""
    decode = {"format": {"kind": "unknown"}}  # no is_ai_generated key
    compose.reject_non_ai_recipes(decode)  # should not raise


# lipsync_eligible


def test_lipsync_eligible_no_decode_signal_allows_lipsync():
    """No decode → trust user's --lipsync flag. Kling will reject faceless
    clips anyway with code 1006, and we fall back gracefully."""
    eligible, reason = compose.lipsync_eligible(None)
    assert eligible is True
    assert "no decode signal" in reason


@pytest.mark.parametrize(
    "kind",
    [
        "talking_head_floating_card",
        "talking_head_with_static_overlay",
        "multi_scene_talking_head",
    ],
)
def test_lipsync_eligible_yes_for_talking_head_kinds(kind):
    eligible, reason = compose.lipsync_eligible({"format": {"kind": kind}})
    assert eligible is True
    assert kind in reason


@pytest.mark.parametrize(
    "kind",
    [
        "greenscreen_kinetic_listicle",
        "ai_montage_kinetic",
        "unknown",
        "static_title_card_montage",
    ],
)
def test_lipsync_eligible_no_for_non_talking_head_kinds(kind):
    eligible, reason = compose.lipsync_eligible({"format": {"kind": kind}})
    assert eligible is False
    assert "disabled" in reason
    assert kind in reason


def test_lipsync_eligible_no_for_missing_format_block():
    """Decode.json with no `format` key → no kind to gate on → conservative,
    refuse lipsync rather than silently pay for a likely-failed cut."""
    eligible, reason = compose.lipsync_eligible({})
    assert eligible is False
