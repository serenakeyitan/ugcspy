"""Tests for scripts.attribute_model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import attribute_model


def test_find_textual_mentions_finds_sora() -> None:
    hits = attribute_model.find_textual_mentions("Made entirely with Sora 2 and Veo 3")
    assert "sora" in hits
    assert "veo" in hits


def test_find_textual_mentions_word_boundary() -> None:
    """Don't match 'sorority' as 'sora'."""
    hits = attribute_model.find_textual_mentions("at the sorority house")
    assert "sora" not in hits


def test_find_textual_mentions_handles_runway_gen3() -> None:
    hits = attribute_model.find_textual_mentions("Runway Gen-3 short")
    assert "runway" in hits


def test_find_textual_mentions_handles_dream_machine() -> None:
    hits = attribute_model.find_textual_mentions("the new Luma Dream Machine")
    assert "luma" in hits


def _scaffold(recipe_dir: Path, *, ai_cut_with_keyframe: bool = True) -> None:
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "source.info.json").write_text(
        json.dumps({"title": "My AI Videos Hit 1M+ Views (Veo3 + Sora 2 Demo)"})
    )
    cuts_a = {
        "index": 0,
        "start_sec": 0.0,
        "end_sec": 2.0,
        "duration_sec": 2.0,
        "flagged_short": False,
        "keyframes": ["cuts/0/a.jpg", "cuts/0/b.jpg", "cuts/0/c.jpg"],
        "inferred_kind": "ai_clip",
        "inferred": {
            "subject": "kangaroo",
            "action": "dance",
            "setting": "stage",
            "style": "3d",
            "camera": "static",
            "lighting": "neon",
            "duration_sec": 2.0,
            "aspect_ratio": "16:9",
            "prompt": "A cartoon kangaroo disco dances",
        },
        "inferred_error": None,
        "transcript": None,
        "ocr_text": None,
        "ocr_confidence": None,
        "paired_prompt_text": None,
    }
    recipe = {
        "schema_version": "0.4",
        "source_url": "https://example.com",
        "video_id": "demo",
        "duration_sec": 2.0,
        "resolution": "1920x1080",
        "fps": 30,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "cuts": [cuts_a],
        "audio": None,
        "assembly": None,
        "model_attribution": None,
    }
    (recipe_dir / "recipe.json").write_text(json.dumps(recipe))
    if ai_cut_with_keyframe:
        for n in ("a", "b", "c"):
            p = recipe_dir / "cuts" / "0" / f"{n}.jpg"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xd8\xff\xe0fake")


def test_attribute_picks_textually_mentioned_model(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    block = attribute_model.attribute(tmp_path)
    # Title says "Veo3 + Sora 2" — both should be candidates; the highest-scored
    # primary depends on tie-break order. Just confirm the block names them.
    assert set(block["candidates"]) == {"sora", "veo"}
    assert block["primary_model"] in {"sora", "veo"}
    assert block["confidence"] >= 0.7


def test_attribute_no_signal_returns_null_primary(tmp_path: Path) -> None:
    _scaffold(tmp_path, ai_cut_with_keyframe=False)
    # Wipe the suggestive title.
    (tmp_path / "source.info.json").write_text(json.dumps({"title": "Some random video"}))
    block = attribute_model.attribute(tmp_path)
    assert block["primary_model"] is None
    assert block["candidates"] == {}


def test_watermark_region_returns_none_without_pil(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the import inside _watermark_region to fail
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name in ("PIL", "PIL.Image", "numpy"):
            raise ImportError(f"simulated missing {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = attribute_model._watermark_region(Path("/tmp/anything.jpg"), "bottom-right")
    assert result is None
