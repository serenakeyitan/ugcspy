"""Tests for scripts.compose.

Covers:
 - recipe-contract handling (#12): prompt resolver across v0.5 + legacy shapes
 - duration semantics (#13): kling_billed_duration + validate_durations
 - decode.json gating (#14): reject_non_ai_recipes + lipsync_eligible
 - caption burn-in (#15): resolve_cut_burnin + wrap + escape + ffmpeg E2E

The pure-compute tests (most of the file) require no external tools and
run in milliseconds. The CI-gated ffmpeg integration tests at the bottom
exercise the actual drawtext invocation; they auto-skip when ffmpeg
lacks libfreetype (some macOS builds) and run in CI where Ubuntu's
ffmpeg ships with freetype by default.

No Kling, no OpenAI, no real Whisper, no real money spent.
"""

from __future__ import annotations

import subprocess

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


# ─── Caption burn-in (issue #15) ───────────────────────────────────────────


def test_resolve_burnin_from_title_cards():
    """Top-level recipe.title_cards entry matching cut.index wins over
    per-cut fields (legacy recipe shape, hand-edited or pre-v0.5)."""
    cut = {"index": 0}
    recipe = {
        "title_cards": [
            {
                "cut_index": 0,
                "ocr_text": "Hello world",
                "presentation": "static_overlay_full_duration",
            }
        ]
    }
    text, presentation = compose.resolve_cut_burnin(cut, recipe)
    assert text == "Hello world"
    assert presentation == "static_overlay_full_duration"


def test_resolve_burnin_from_cut_caption_when_no_title_card():
    """v0.5 canonical: cut.caption is the editorial overlay layer."""
    cut = {"index": 1, "caption": "Buy now"}
    recipe = {"title_cards": []}
    text, presentation = compose.resolve_cut_burnin(cut, recipe)
    assert text == "Buy now"
    assert presentation == "static_overlay_full_duration"


def test_resolve_burnin_from_cut_ocr_text_when_no_caption():
    """v0.5 fallback: cut.ocr_text is raw OCR from the source frames."""
    cut = {"index": 1, "ocr_text": "SALE 50% OFF"}
    recipe = {}
    text, presentation = compose.resolve_cut_burnin(cut, recipe)
    assert text == "SALE 50% OFF"


def test_resolve_burnin_title_card_takes_priority_over_per_cut():
    """When BOTH title_cards and cut.caption exist, title_cards wins
    (it's the more specific overlay-text annotation)."""
    cut = {"index": 0, "caption": "fallback caption", "ocr_text": "fallback ocr"}
    recipe = {
        "title_cards": [
            {"cut_index": 0, "ocr_text": "title card wins", "presentation": "kinetic_per_chunk"}
        ]
    }
    text, presentation = compose.resolve_cut_burnin(cut, recipe)
    assert text == "title card wins"
    assert presentation == "kinetic_per_chunk"


def test_resolve_burnin_returns_none_when_no_overlay_text():
    cut = {"index": 0}
    recipe = {"title_cards": []}
    text, presentation = compose.resolve_cut_burnin(cut, recipe)
    assert text is None
    assert presentation == ""


def test_resolve_burnin_skips_title_card_for_wrong_cut_index():
    """A title_cards entry for cut_index=2 shouldn't burn into cut 0."""
    cut = {"index": 0, "caption": "use cut caption"}
    recipe = {"title_cards": [{"cut_index": 2, "ocr_text": "for different cut"}]}
    text, _ = compose.resolve_cut_burnin(cut, recipe)
    assert text == "use cut caption"


# wrap_burnin_text


def test_wrap_burnin_text_handles_single_line():
    out = compose.wrap_burnin_text("Hello world", columns=30)
    assert out == "Hello world"


def test_wrap_burnin_text_wraps_long_line():
    long = "This is a very long sentence that should wrap across multiple lines for mobile readability"
    out = compose.wrap_burnin_text(long, columns=30)
    lines = out.split("\n")
    # Every line should be ≤30 chars (or one word if a word is longer)
    for line in lines:
        # break_long_words=False means words longer than columns can exceed
        assert len(line) <= 60, f"line too long: {line!r}"
    assert len(lines) >= 3  # roughly 90 chars / 30 cols


def test_wrap_burnin_text_preserves_existing_newlines():
    """When the recipe pre-formats with \\n (numbered lists, headlines),
    those breaks should be respected, not joined into a single block."""
    out = compose.wrap_burnin_text("Top 5 tips:\n\n1. First\n2. Second", columns=30)
    # The "Top 5 tips:" line is its own paragraph; the empty line preserves
    # the blank between headline and list
    lines = out.split("\n")
    assert "Top 5 tips:" in lines
    assert "1. First" in lines
    assert "2. Second" in lines


def test_wrap_burnin_text_truncates_to_max_lines():
    """An overlay that wraps to many lines covers the frame; truncate with …"""
    very_long = "word " * 200  # 200 words, will wrap to many lines
    out = compose.wrap_burnin_text(very_long, columns=30, max_lines=5)
    lines = out.split("\n")
    assert len(lines) == 5
    assert lines[-1] == "…"


def test_wrap_burnin_text_handles_empty():
    assert compose.wrap_burnin_text("") == ""


# escape_drawtext


def test_escape_drawtext_passes_plain_text():
    assert compose.escape_drawtext("Hello world") == "Hello world"


@pytest.mark.parametrize(
    "raw,expected_contains",
    [
        # The escape function adds backslashes; we verify the special char
        # is now preceded by one (ffmpeg parses these out).
        ("path:to:something", "path\\:to\\:something"),
        ("it's a test", "it\\'s a test"),
        ("a,b,c", "a\\,b\\,c"),
        ("100% off", "100\\% off"),
        ("[bracket]", "\\[bracket\\]"),
    ],
)
def test_escape_drawtext_escapes_special_chars(raw, expected_contains):
    assert expected_contains in compose.escape_drawtext(raw)


def test_escape_drawtext_handles_backslashes_first():
    """Backslash must be escaped FIRST so we don't double-escape the
    backslashes we add for other special chars."""
    # A raw backslash should become double backslash
    assert compose.escape_drawtext("a\\b") == "a\\\\b"


# build_drawtext_filter


def test_build_drawtext_filter_includes_text():
    out = compose.build_drawtext_filter("Hello", clip_dur=5.0)
    assert "Hello" in out
    assert out.startswith("drawtext=")


def test_build_drawtext_filter_escapes_special_chars():
    out = compose.build_drawtext_filter("it's: 50%", clip_dur=5.0)
    # The output should not contain the raw special chars in a way that
    # would break filtergraph parsing. Specifically: apostrophe + colon
    # + percent should be escaped.
    assert "it\\'s" in out
    assert "\\:" in out
    assert "\\%" in out


def test_build_drawtext_filter_renders_styling():
    out = compose.build_drawtext_filter("Hi", clip_dur=5.0)
    # Verify the key styling args are present so a regression in
    # render style breaks the test
    assert "fontcolor=white" in out
    assert "fontsize=42" in out
    assert "box=1" in out  # background box for legibility
    assert "x=(w-text_w)/2" in out  # centered horizontally


# ─── Burn-in ffmpeg integration (CI-gated) ────────────────────────────────
#
# These tests exercise the actual ffmpeg drawtext invocation end-to-end.
# Skipped on environments where ffmpeg lacks libfreetype (e.g. some
# macOS Homebrew builds). Ubuntu apt-installed ffmpeg ships with
# freetype, so CI runs these.


@pytest.fixture
def synthetic_clip(tmp_path):
    """Generate a 5s color test clip via ffmpeg for burn-in integration."""
    out = tmp_path / "synthetic.mp4"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=teal:1080x1920:duration=5:rate=30",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"ffmpeg not available for fixture generation: {proc.stderr[:200]}")
    return out


@pytest.mark.skipif(not compose.drawtext_available(), reason="ffmpeg lacks libfreetype")
def test_burnin_produces_video_with_correct_duration(synthetic_clip, tmp_path):
    """End-to-end: feed a synthetic clip + drawtext filter through
    mix_clip_with_silence, verify the output is the right duration and
    contains video stream. Confirms the filter syntax is valid and the
    drawtext call actually completes."""
    out = tmp_path / "burned.mp4"
    text = compose.wrap_burnin_text("TOP 5 TIPS\nfor better conversation", columns=30)
    filter_spec = compose.build_drawtext_filter(text, clip_dur=5.0)
    compose.mix_clip_with_silence(synthetic_clip, out, clip_dur=5.0, burnin=filter_spec)
    assert out.exists()
    duration = compose.ffprobe_duration(out)
    assert 4.9 < duration < 5.1, f"expected ~5s, got {duration}"


@pytest.mark.skipif(not compose.drawtext_available(), reason="ffmpeg lacks libfreetype")
def test_burnin_handles_apostrophes_and_special_chars(synthetic_clip, tmp_path):
    """Real UGC overlay text has apostrophes, percent signs, commas, etc.
    Regression test for escape_drawtext."""
    out = tmp_path / "burned-special.mp4"
    tricky_text = "Save 50% off — it's a steal, today only!"
    wrapped = compose.wrap_burnin_text(tricky_text)
    filter_spec = compose.build_drawtext_filter(wrapped, clip_dur=5.0)
    # Should not raise — the escape function is doing its job
    compose.mix_clip_with_silence(synthetic_clip, out, clip_dur=5.0, burnin=filter_spec)
    assert out.exists()
    duration = compose.ffprobe_duration(out)
    assert 4.9 < duration < 5.1


@pytest.mark.skipif(not compose.drawtext_available(), reason="ffmpeg lacks libfreetype")
def test_burnin_produces_no_op_when_text_empty(synthetic_clip, tmp_path):
    """No burnin → no filter applied → output still correct."""
    out = tmp_path / "no-burnin.mp4"
    compose.mix_clip_with_silence(synthetic_clip, out, clip_dur=5.0, burnin=None)
    assert out.exists()
    duration = compose.ffprobe_duration(out)
    assert 4.9 < duration < 5.1


# ─── Resume / idempotency (issue #16) ──────────────────────────────────────


@pytest.fixture
def _minimal_args():
    """argparse.Namespace stand-in with just the fields the state helpers read."""
    import argparse

    return argparse.Namespace(lipsync=False, no_burnin=False, no_resume=False)


def _minimal_recipe():
    """Two-cut recipe for state tests."""
    return {
        "schema_version": "0.5",
        "video_id": "test",
        "source_url": "https://example.com/v",
        "duration_sec": 10,
        "generated_at": "2026-05-25T00:00:00Z",
        "cuts": [
            {"index": 0, "inferred": {"prompt": "scene 0"}, "duration_sec": 5},
            {"index": 1, "inferred": {"prompt": "scene 1"}, "duration_sec": 5},
        ],
    }


# compute_recipe_hash


def test_recipe_hash_stable_across_runs():
    r = _minimal_recipe()
    assert compose.compute_recipe_hash(r) == compose.compute_recipe_hash(r)


def test_recipe_hash_changes_when_prompt_changes():
    r1 = _minimal_recipe()
    r2 = _minimal_recipe()
    r2["cuts"][0]["inferred"]["prompt"] = "different scene"
    assert compose.compute_recipe_hash(r1) != compose.compute_recipe_hash(r2)


def test_recipe_hash_stable_when_only_editorial_changes():
    """source_url / generated_at don't affect rendering — they shouldn't
    invalidate the cache."""
    r1 = _minimal_recipe()
    r2 = _minimal_recipe()
    r2["source_url"] = "https://different.com/v"
    r2["generated_at"] = "2099-12-31T23:59:59Z"
    assert compose.compute_recipe_hash(r1) == compose.compute_recipe_hash(r2)


def test_recipe_hash_changes_when_tts_changes():
    r1 = _minimal_recipe()
    r1["tts"] = {"script": "Hello", "language": "en", "duration_sec": 1.0, "likely_synthetic": True}
    r2 = _minimal_recipe()
    r2["tts"] = {"script": "Goodbye", "language": "en", "duration_sec": 1.0, "likely_synthetic": True}
    assert compose.compute_recipe_hash(r1) != compose.compute_recipe_hash(r2)


def test_recipe_hash_independent_of_key_order():
    """JSON dict iteration order shouldn't affect the hash."""
    r1 = {"cuts": [{"index": 0, "inferred": {"prompt": "x"}}]}
    r2 = {"cuts": [{"inferred": {"prompt": "x"}, "index": 0}]}
    assert compose.compute_recipe_hash(r1) == compose.compute_recipe_hash(r2)


# args_signature


def test_args_signature_changes_with_lipsync_flag(_minimal_args):
    sig1 = compose.args_signature(_minimal_args)
    _minimal_args.lipsync = True
    sig2 = compose.args_signature(_minimal_args)
    assert sig1 != sig2


def test_args_signature_changes_with_no_burnin_flag(_minimal_args):
    sig1 = compose.args_signature(_minimal_args)
    _minimal_args.no_burnin = True
    sig2 = compose.args_signature(_minimal_args)
    assert sig1 != sig2


# load_state / save_state


def test_load_state_returns_none_when_no_file(tmp_path):
    assert compose.load_state(tmp_path) is None


def test_save_then_load_roundtrips(tmp_path):
    state = {"schema_version": "1", "recipe_hash": "sha256:abc", "total_cost": 1.5, "cuts": []}
    compose.save_state(tmp_path, state)
    loaded = compose.load_state(tmp_path)
    assert loaded == state


def test_save_state_is_atomic(tmp_path):
    """A partially-written state file (simulated by writing then crashing
    mid-write) shouldn't leave the actual state file corrupt — we write
    to a .tmp first and rename. After save_state, no .tmp leftover."""
    state = {"schema_version": "1", "cuts": [], "total_cost": 0.0}
    compose.save_state(tmp_path, state)
    sp = compose.state_path(tmp_path)
    assert sp.exists()
    assert not sp.with_suffix(sp.suffix + ".tmp").exists()


def test_load_state_handles_corrupt_json(tmp_path, capsys):
    sp = compose.state_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not valid json {{{")
    result = compose.load_state(tmp_path)
    assert result is None
    err = capsys.readouterr().err
    assert "unreadable" in err


# init_or_load_state


def test_init_or_load_state_fresh_when_no_existing(_minimal_args, tmp_path):
    r = _minimal_recipe()
    state, resumed = compose.init_or_load_state(tmp_path, r, _minimal_args)
    assert resumed == 0
    assert state["schema_version"] == "1"
    assert state["recipe_hash"] == compose.compute_recipe_hash(r)
    assert state["total_cost"] == 0.0
    assert len(state["cuts"]) == 2
    assert all(c == {"index": i, "text2video": {}, "tts": {}, "lipsync": {}} for i, c in enumerate(state["cuts"]))


def test_init_or_load_state_resumes_matching_state(_minimal_args, tmp_path, capsys):
    r = _minimal_recipe()
    # Pre-populate state.json as if a previous run completed cut 0
    prior = {
        "schema_version": "1",
        "recipe_hash": compose.compute_recipe_hash(r),
        "args_signature": compose.args_signature(_minimal_args),
        "total_cost": 0.5,
        "cuts": [
            {"index": 0, "text2video": {"status": "done", "cost": 0.5}, "tts": {}, "lipsync": {}},
            {"index": 1, "text2video": {}, "tts": {}, "lipsync": {}},
        ],
    }
    compose.save_state(tmp_path, prior)
    state, resumed = compose.init_or_load_state(tmp_path, r, _minimal_args)
    assert resumed == 1
    assert state["total_cost"] == 0.5
    assert state["cuts"][0]["text2video"]["status"] == "done"
    out = capsys.readouterr().out
    assert "resuming" in out


def test_init_or_load_state_refuses_when_recipe_hash_mismatched(_minimal_args, tmp_path, capsys):
    r = _minimal_recipe()
    prior = {
        "schema_version": "1",
        "recipe_hash": "sha256:WRONG",  # stale hash from a prior recipe
        "args_signature": compose.args_signature(_minimal_args),
        "total_cost": 0.5,
        "cuts": [],
    }
    compose.save_state(tmp_path, prior)
    with pytest.raises(SystemExit) as exc:
        compose.init_or_load_state(tmp_path, r, _minimal_args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "recipe.json has changed" in err
    assert "--no-resume" in err  # tells the user how to override


def test_init_or_load_state_refuses_when_args_signature_mismatched(_minimal_args, tmp_path, capsys):
    r = _minimal_recipe()
    prior = {
        "schema_version": "1",
        "recipe_hash": compose.compute_recipe_hash(r),
        "args_signature": "lipsync=True|no_burnin=False",  # was True, now False
        "total_cost": 0.5,
        "cuts": [],
    }
    compose.save_state(tmp_path, prior)
    with pytest.raises(SystemExit) as exc:
        compose.init_or_load_state(tmp_path, r, _minimal_args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "args changed" in err
    assert "--no-resume" in err


def test_init_or_load_state_no_resume_discards(_minimal_args, tmp_path, capsys):
    r = _minimal_recipe()
    # Even a perfectly-matching state should be discarded when --no-resume.
    prior = {
        "schema_version": "1",
        "recipe_hash": compose.compute_recipe_hash(r),
        "args_signature": compose.args_signature(_minimal_args),
        "total_cost": 99.0,
        "cuts": [{"index": 0, "text2video": {"status": "done", "cost": 99.0}, "tts": {}, "lipsync": {}}],
    }
    compose.save_state(tmp_path, prior)
    _minimal_args.no_resume = True
    state, resumed = compose.init_or_load_state(tmp_path, r, _minimal_args)
    assert resumed == 0
    assert state["total_cost"] == 0.0  # fresh state, prior $99 discarded
    out = capsys.readouterr().out
    assert "--no-resume" in out


def test_init_or_load_state_handles_schema_version_drift(_minimal_args, tmp_path, capsys):
    r = _minimal_recipe()
    prior = {
        "schema_version": "0",  # ancient
        "recipe_hash": compose.compute_recipe_hash(r),
        "total_cost": 5.0,
        "cuts": [],
    }
    compose.save_state(tmp_path, prior)
    state, resumed = compose.init_or_load_state(tmp_path, r, _minimal_args)
    # Old schema → discarded fresh. Better than failing on a stale
    # state-file shape from an older compose build.
    assert resumed == 0
    assert state["total_cost"] == 0.0
    err = capsys.readouterr().err
    assert "schema mismatch" in err


# stage_done


def test_stage_done_true_when_marked():
    state = {"cuts": [{"index": 0, "text2video": {"status": "done", "cost": 0.5}}]}
    assert compose.stage_done(state, 0, "text2video") is True


def test_stage_done_false_when_not_marked():
    state = {"cuts": [{"index": 0, "text2video": {}}]}
    assert compose.stage_done(state, 0, "text2video") is False


def test_stage_done_false_for_unknown_cut():
    state = {"cuts": []}
    assert compose.stage_done(state, 0, "text2video") is False


def test_stage_done_false_for_unknown_stage():
    state = {"cuts": [{"index": 0}]}
    assert compose.stage_done(state, 0, "lipsync") is False


# record_stage


def test_record_stage_persists_to_disk(tmp_path):
    state = {
        "schema_version": "1",
        "total_cost": 0.0,
        "cuts": [{"index": 0, "text2video": {}, "tts": {}, "lipsync": {}}],
    }
    compose.record_stage(state, tmp_path, 0, "text2video", 0.5, external_id="task-abc")
    assert state["total_cost"] == 0.5
    assert state["cuts"][0]["text2video"]["status"] == "done"
    assert state["cuts"][0]["text2video"]["cost"] == 0.5
    assert state["cuts"][0]["text2video"]["external_id"] == "task-abc"
    # And persisted
    loaded = compose.load_state(tmp_path)
    assert loaded == state


def test_record_stage_invalidates_downstream_lipsync(tmp_path, capsys):
    """Re-running text2video must invalidate the lipsync cache for the
    same cut — lipsync was warped against the OLD text2video output."""
    state = {
        "schema_version": "1",
        "total_cost": 1.0,
        "cuts": [
            {
                "index": 0,
                "text2video": {"status": "done", "cost": 0.5},
                "tts": {},
                "lipsync": {"status": "done", "cost": 0.5},
            }
        ],
    }
    # Re-run text2video
    compose.record_stage(state, tmp_path, 0, "text2video", 0.5)
    assert state["cuts"][0]["text2video"]["status"] == "done"
    # lipsync entry should be cleared so it re-runs next time
    assert state["cuts"][0]["lipsync"] == {}
    err = capsys.readouterr().err
    assert "invalidating lipsync" in err


def test_record_stage_invalidates_lipsync_when_tts_reruns(tmp_path):
    """Lipsync uses TTS audio as input. Re-running TTS means lipsync was
    warped against stale audio."""
    state = {
        "schema_version": "1",
        "total_cost": 1.0,
        "cuts": [
            {
                "index": 0,
                "text2video": {"status": "done", "cost": 0.5},
                "tts": {"status": "done", "cost": 0.001},
                "lipsync": {"status": "done", "cost": 0.42},
            }
        ],
    }
    compose.record_stage(state, tmp_path, 0, "tts", 0.001)
    assert state["cuts"][0]["lipsync"] == {}
