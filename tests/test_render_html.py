"""Tests for scripts.render_html."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import render_html


def _make_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal-but-valid JPEG header so the base64 path doesn't blow up.
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")


def _write_recipe(tmp_path: Path, recipe: dict) -> Path:
    """Write recipe.json + dummy keyframe files referenced by it."""
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    for cut in recipe.get("cuts", []):
        for kf in cut.get("keyframes", []):
            _make_jpeg(recipe_dir / kf)
    recipe_path = recipe_dir / "recipe.json"
    recipe_path.write_text(json.dumps(recipe))
    return recipe_path


def _full_inferred() -> dict:
    return {
        "subject": "subject",
        "action": "action",
        "setting": "setting",
        "style": "photoreal cinematic",
        "camera": "static medium",
        "lighting": "golden hour",
        "duration_sec": 2.0,
        "aspect_ratio": "16:9",
        "prompt": "A test prompt for the rendering.",
    }


def _minimal_recipe() -> dict:
    return {
        "schema_version": "0.5",
        "source_url": "https://example.com/v/42",
        "video_id": "test-vid",
        "duration_sec": 4.0,
        "resolution": "1920x1080",
        "fps": 30,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "hook": None,
        "cuts": [
            {
                "index": 0,
                "start_sec": 0.0,
                "end_sec": 2.0,
                "duration_sec": 2.0,
                "flagged_short": False,
                "keyframes": ["cuts/0/a.jpg", "cuts/0/b.jpg", "cuts/0/c.jpg"],
                "inferred_kind": "ai_clip",
                "inferred": _full_inferred(),
                "inferred_error": None,
                "transcript": "Hello world",
                "ocr_text": None,
                "ocr_confidence": None,
                "paired_prompt_text": None,
                "caption": "WAIT FOR IT",
            }
        ],
        "audio": None,
        "tts": None,
        "assembly": None,
        "model_attribution": None,
    }


def test_render_writes_html_file(tmp_path: Path) -> None:
    recipe_path = _write_recipe(tmp_path, _minimal_recipe())
    html = render_html.render(recipe_path)
    assert "<!doctype html>" in html.lower()
    assert "test-vid" in html
    assert "ai_clip" in html
    assert "A test prompt for the rendering." in html
    assert "WAIT FOR IT" in html
    assert "Hello world" in html


def test_render_embeds_keyframe_as_data_url(tmp_path: Path) -> None:
    """The 'b' frame should be inlined as a data:image/jpeg;base64 URL."""
    recipe_path = _write_recipe(tmp_path, _minimal_recipe())
    html = render_html.render(recipe_path)
    assert "data:image/jpeg;base64," in html


def test_render_handles_null_hook(tmp_path: Path) -> None:
    recipe = _minimal_recipe()
    recipe["hook"] = None
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    # Hook section still rendered, but with the null-state copy.
    assert "Hook" in html
    assert "No clear hook" in html


def test_render_renders_full_hook_block(tmp_path: Path) -> None:
    recipe = _minimal_recipe()
    recipe["hook"] = {
        "duration_sec": 3.5,
        "spans_cuts": [0],
        "pattern": "claim",
        "text": "Money",
        "voiceover": "Some people know how to live better than others",
        "first_visual": "real-footage description",
    }
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert "claim" in html
    assert "Money" in html
    assert "Some people know how to live better than others" in html
    assert "real-footage description" in html


def test_render_renders_tts_block_with_evidence(tmp_path: Path) -> None:
    recipe = _minimal_recipe()
    recipe["tts"] = {
        "script": "the full script",
        "language": "en",
        "duration_sec": 16.62,
        "likely_synthetic": False,
        "evidence": ["natural laughter", "uneven pacing"],
        "model": None,
        "voice_id": None,
    }
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert "Real human speech" in html
    assert "natural laughter" in html
    assert "uneven pacing" in html
    assert "the full script" in html


def test_render_renders_synthetic_tts_badge(tmp_path: Path) -> None:
    recipe = _minimal_recipe()
    recipe["tts"] = {
        "script": "...",
        "language": "en",
        "duration_sec": 60.0,
        "likely_synthetic": True,
        "evidence": ["zero filler words"],
        "model": None,
        "voice_id": None,
    }
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert "AI-generated TTS" in html


def test_render_renders_attribution_with_candidates(tmp_path: Path) -> None:
    recipe = _minimal_recipe()
    recipe["model_attribution"] = {
        "primary_model": "sora",
        "confidence": 1.0,
        "evidence": ["title mentions Sora 2"],
        "per_cut": {},
        "candidates": {"sora": 1.0, "veo": 0.7},
    }
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert "sora" in html
    # both candidates rendered as chips
    assert "veo" in html
    assert "title mentions Sora 2" in html


def test_render_renders_null_kind_cut_with_error(tmp_path: Path) -> None:
    recipe = _minimal_recipe()
    recipe["cuts"][0] = {
        "index": 0,
        "start_sec": 0.0,
        "end_sec": 2.0,
        "duration_sec": 2.0,
        "flagged_short": False,
        "keyframes": ["cuts/0/a.jpg", "cuts/0/b.jpg", "cuts/0/c.jpg"],
        "inferred_kind": "non_ai_footage",
        "inferred": None,
        "inferred_error": "real human on camera",
        "transcript": None,
        "ocr_text": None,
        "ocr_confidence": None,
        "paired_prompt_text": None,
        "caption": None,
    }
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert "non_ai_footage" in html
    assert "real human on camera" in html


def test_render_handles_missing_keyframes(tmp_path: Path) -> None:
    """If a cut references a keyframe that doesn't exist on disk, the renderer
    shouldn't crash — it just emits a thumb-less cut."""
    recipe = _minimal_recipe()
    recipe_path = _write_recipe(tmp_path, recipe)
    # Delete the keyframes after writing the recipe.
    for kf in recipe["cuts"][0]["keyframes"]:
        (recipe_path.parent / kf).unlink()
    html = render_html.render(recipe_path)
    # No data: URL, but body still renders.
    assert "data:image/jpeg" not in html
    assert "ai_clip" in html


def test_render_escapes_html_in_inputs(tmp_path: Path) -> None:
    """User-controlled fields (prompt, caption) must be HTML-escaped."""
    recipe = _minimal_recipe()
    recipe["cuts"][0]["inferred"]["prompt"] = '<script>alert("xss")</script>'
    recipe["cuts"][0]["caption"] = "<img src=x onerror=alert(1)>"
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_main_writes_to_default_out_path(tmp_path: Path) -> None:
    recipe_path = _write_recipe(tmp_path, _minimal_recipe())
    rc = render_html.main([str(recipe_path)])
    assert rc == 0
    assert (recipe_path.parent / "recipe.html").exists()


def test_main_writes_to_custom_out_path(tmp_path: Path) -> None:
    recipe_path = _write_recipe(tmp_path, _minimal_recipe())
    custom = tmp_path / "custom.html"
    rc = render_html.main([str(recipe_path), "--out", str(custom)])
    assert rc == 0
    assert custom.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("transcript", "voiceover quoted in the cut"),
        ("paired_prompt_text", "ground-truth from a title card"),
        ("caption", "kinetic typography text"),
    ],
)
def test_render_includes_optional_per_cut_fields(tmp_path: Path, field: str, value: str) -> None:
    recipe = _minimal_recipe()
    recipe["cuts"][0][field] = value
    recipe_path = _write_recipe(tmp_path, recipe)
    html = render_html.render(recipe_path)
    assert value in html
