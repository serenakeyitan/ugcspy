"""Tests for scripts.assemble_recipe."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts import assemble_recipe

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0fake")


CANNED_INFERRED = {
    "subject": "subject",
    "action": "action",
    "setting": "setting",
    "style": "style",
    "camera": "camera",
    "lighting": "lighting",
    "duration_sec": 2.0,
    "aspect_ratio": "16:9",
    "prompt": "test prompt",
}


def _scaffold_recipe_dir(root: Path, n_cuts: int = 2, with_inferred: bool = True) -> Path:
    recipe_dir = root / "recipe"
    recipe_dir.mkdir()
    (recipe_dir / "source.info.json").write_text(
        json.dumps(
            {
                "id": "vid42",
                "duration": float(n_cuts) * 2.0,
                "fps": 24,
                "width": 1920,
                "height": 1080,
            }
        )
    )
    cuts = []
    for i in range(n_cuts):
        cuts.append(
            {
                "index": i,
                "start_sec": i * 2.0,
                "end_sec": (i + 1) * 2.0,
                "duration_sec": 2.0,
                "flagged_short": False,
            }
        )
        for n in ("a", "b", "c"):
            _make_jpeg(recipe_dir / "cuts" / str(i) / f"{n}.jpg")
        if with_inferred:
            (recipe_dir / "cuts" / str(i) / "inferred.json").write_text(json.dumps(CANNED_INFERRED))
    (recipe_dir / "cuts.json").write_text(json.dumps(cuts))
    return recipe_dir


def test_assemble_writes_valid_recipe(tmp_path: Path) -> None:
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    out = assemble_recipe.assemble(
        source_url="https://example.com/v/42",
        recipe_dir=recipe_dir,
        repo_root=REPO_ROOT,
        now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    data = json.loads(out.read_text())

    assert data["schema_version"] == "0.5"
    assert data["source_url"] == "https://example.com/v/42"
    assert data["video_id"] == "vid42"
    assert data["resolution"] == "1920x1080"
    assert data["fps"] == 24
    assert data["duration_sec"] == 4.0
    assert len(data["cuts"]) == 2
    for cut in data["cuts"]:
        assert cut["keyframes"] == [f"cuts/{cut['index']}/{n}.jpg" for n in ("a", "b", "c")]
        assert cut["inferred_kind"] == "ai_clip"
        assert cut["inferred"]["prompt"] == "test prompt"
        assert cut["inferred_error"] is None


def test_assemble_classifies_each_null_kind(tmp_path: Path) -> None:
    """Each of the structured null kinds passes through correctly."""
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=5)
    kinds = ["title_card", "non_ai_footage", "lumped_cuts", "transition", "unreadable"]
    for i, kind in enumerate(kinds):
        (recipe_dir / "cuts" / str(i) / "inferred.json").write_text(
            json.dumps({"inferred_kind": kind, "error": f"reason for {kind}"})
        )
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    for i, kind in enumerate(kinds):
        assert data["cuts"][i]["inferred_kind"] == kind
        assert data["cuts"][i]["inferred"] is None
        assert data["cuts"][i]["inferred_error"] == f"reason for {kind}"


def test_assemble_back_compat_legacy_null_shape(tmp_path: Path) -> None:
    """Legacy {'inferred': null, 'error': ...} files still work, mapped to unreadable."""
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    (recipe_dir / "cuts" / "0" / "inferred.json").write_text(
        json.dumps({"inferred": None, "error": "old-style failure"})
    )
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["cuts"][0]["inferred_kind"] == "unreadable"
    assert data["cuts"][0]["inferred"] is None
    assert data["cuts"][0]["inferred_error"] == "old-style failure"
    # Other cut still ai_clip
    assert data["cuts"][1]["inferred_kind"] == "ai_clip"


def test_assemble_rejects_unknown_kind(tmp_path: Path) -> None:
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    (recipe_dir / "cuts" / "0" / "inferred.json").write_text(
        json.dumps({"inferred_kind": "garbage", "error": "..."})
    )
    with pytest.raises(ValueError, match="unknown inferred_kind"):
        assemble_recipe.assemble("https://example.com/x", recipe_dir, REPO_ROOT)


def test_assemble_includes_audio_block_and_per_cut_transcripts(tmp_path: Path) -> None:
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=2)
    # Drop a transcript at recipe root + per-cut transcript files.
    (recipe_dir / "transcript.json").write_text(
        json.dumps(
            {
                "language": "en",
                "duration_sec": 4.0,
                "segments": [],
                "words": [],
            }
        )
    )
    (recipe_dir / "cuts" / "0" / "transcript.json").write_text(
        json.dumps({"start_sec": 0.0, "end_sec": 2.0, "text": "Hello world", "words": []})
    )
    (recipe_dir / "cuts" / "1" / "transcript.json").write_text(
        json.dumps({"start_sec": 2.0, "end_sec": 4.0, "text": "", "words": []})
    )

    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())

    assert data["audio"] == {
        "language": "en",
        "transcript_path": "transcript.json",
        "duration_sec": 4.0,
    }
    assert data["cuts"][0]["transcript"] == "Hello world"
    # Empty-string transcript becomes None (no spoken content in this cut).
    assert data["cuts"][1]["transcript"] is None


def test_assemble_audio_block_null_when_no_root_transcript(tmp_path: Path) -> None:
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["audio"] is None
    for c in data["cuts"]:
        assert c["transcript"] is None


def test_assemble_pairs_title_card_to_following_cut(tmp_path: Path) -> None:
    """A title_card cut with OCR text annotates the next cut's paired_prompt_text."""
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=2)
    # Re-classify cut 0 as title_card (via inferred.json) and add OCR text for it.
    (recipe_dir / "cuts" / "0" / "inferred.json").write_text(
        json.dumps({"inferred_kind": "title_card", "error": "title card"})
    )
    (recipe_dir / "cuts" / "0" / "ocr.json").write_text(
        json.dumps(
            {
                "text": "A litter of golden retriever puppies playing in the snow",
                "confidence": 88.0,
                "n_words": 9,
                "bg_std": 12.0,
                "is_title_card": True,
            }
        )
    )
    # Cut 1 stays ai_clip from the scaffold's CANNED_INFERRED.

    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["cuts"][0]["inferred_kind"] == "title_card"
    assert data["cuts"][0]["ocr_text"].startswith("A litter of golden retriever")
    assert data["cuts"][0]["paired_prompt_text"] is None  # title cards don't get paired backwards
    # The clip after the title card carries the ground-truth prompt.
    assert data["cuts"][1]["paired_prompt_text"] == data["cuts"][0]["ocr_text"]


def test_assemble_no_pairing_without_ocr(tmp_path: Path) -> None:
    """When OCR didn't run, paired_prompt_text stays null on every cut."""
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=2)
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    for c in data["cuts"]:
        assert c["ocr_text"] is None
        assert c["paired_prompt_text"] is None


def test_assemble_lifts_caption_from_inferred_to_cut_top_level(tmp_path: Path) -> None:
    """Agent writes caption inside inferred.json's prompt object; assembler
    lifts it to the cut top level so it sits next to transcript/ocr_text."""
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=2)
    # Add caption to cut 0's inferred object.
    cut0_inferred = dict(CANNED_INFERRED)
    cut0_inferred["caption"] = "WAIT FOR IT..."
    (recipe_dir / "cuts" / "0" / "inferred.json").write_text(json.dumps(cut0_inferred))
    # Cut 1 keeps no caption.

    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["cuts"][0]["caption"] == "WAIT FOR IT..."
    # The lifted caption is removed from the embedded inferred object.
    assert "caption" not in data["cuts"][0]["inferred"]
    assert data["cuts"][1]["caption"] is None


def test_assemble_caption_null_when_agent_omits(tmp_path: Path) -> None:
    """If the agent's inferred object has no caption key, cut.caption is null."""
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=1)
    # CANNED_INFERRED has no caption.
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["cuts"][0]["caption"] is None


def test_assemble_caption_null_for_non_ai_cuts(tmp_path: Path) -> None:
    """A title_card cut without an inferred object stays caption-null."""
    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=1)
    (recipe_dir / "cuts" / "0" / "inferred.json").write_text(
        json.dumps({"inferred_kind": "title_card", "error": "text only"})
    )
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["cuts"][0]["caption"] is None


def test_assemble_emits_hook_and_tts_as_null_when_files_absent(tmp_path: Path) -> None:
    """No hook.json or tts.json => both top-level fields are null."""
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["hook"] is None
    assert data["tts"] is None


def test_assemble_reads_hook_from_hook_json(tmp_path: Path) -> None:
    """When hook.json contains a full hook object, it's embedded verbatim."""
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    hook_obj = {
        "duration_sec": 4.0,
        "spans_cuts": [0],
        "pattern": "question",
        "text": "Exact Workflow / for how to come up",
        "voiceover": "What if I can show you the exact workflow",
        "first_visual": None,
    }
    (recipe_dir / "hook.json").write_text(json.dumps(hook_obj))
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["hook"] == hook_obj


def test_assemble_handles_explicit_no_hook_marker(tmp_path: Path) -> None:
    """Agent writes {'hook': null} for videos without a clear hook."""
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    (recipe_dir / "hook.json").write_text(json.dumps({"hook": None}))
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["hook"] is None


def test_assemble_validates_hook_pattern_enum(tmp_path: Path) -> None:
    """A hook.json with an invalid pattern fails schema validation."""
    import jsonschema

    recipe_dir = _scaffold_recipe_dir(tmp_path)
    bad_hook = {
        "duration_sec": 2.0,
        "spans_cuts": [0],
        "pattern": "fake_pattern_not_in_enum",
    }
    (recipe_dir / "hook.json").write_text(json.dumps(bad_hook))
    with pytest.raises(jsonschema.ValidationError):
        assemble_recipe.assemble("https://example.com/x", recipe_dir, REPO_ROOT)


def test_assemble_reads_tts_from_tts_json(tmp_path: Path) -> None:
    """When tts.json contains a full block, it's embedded verbatim."""
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    tts_obj = {
        "script": "What if I can show you the exact workflow",
        "language": "en",
        "duration_sec": 19.44,
        "likely_synthetic": False,
        "evidence": ["natural laughter", "uneven pacing"],
        "model": None,
        "voice_id": None,
    }
    (recipe_dir / "tts.json").write_text(json.dumps(tts_obj))
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["tts"] == tts_obj


def test_assemble_handles_explicit_no_tts_marker(tmp_path: Path) -> None:
    """Agent writes {'tts': null} for silent / no-voiceover videos."""
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    (recipe_dir / "tts.json").write_text(json.dumps({"tts": None}))
    out = assemble_recipe.assemble(
        "https://example.com/x", recipe_dir, REPO_ROOT, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    data = json.loads(out.read_text())
    assert data["tts"] is None


def test_assemble_validates_tts_required_fields(tmp_path: Path) -> None:
    """A tts.json missing the required likely_synthetic field fails schema validation."""
    import jsonschema

    recipe_dir = _scaffold_recipe_dir(tmp_path)
    bad_tts = {
        "script": "x",
        "language": "en",
        "duration_sec": 1.0,
        # Missing 'likely_synthetic' — required by the schema for tts when not null
    }
    (recipe_dir / "tts.json").write_text(json.dumps(bad_tts))
    with pytest.raises(jsonschema.ValidationError):
        assemble_recipe.assemble("https://example.com/x", recipe_dir, REPO_ROOT)


def test_assemble_raises_on_missing_keyframe(tmp_path: Path) -> None:
    recipe_dir = _scaffold_recipe_dir(tmp_path)
    (recipe_dir / "cuts" / "0" / "a.jpg").unlink()
    with pytest.raises(FileNotFoundError, match="missing keyframe"):
        assemble_recipe.assemble("https://example.com/x", recipe_dir, REPO_ROOT)


def test_assemble_raises_on_missing_cuts_json(tmp_path: Path) -> None:
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="cuts.json"):
        assemble_recipe.assemble("https://example.com/x", recipe_dir, REPO_ROOT)


def test_assemble_validates_against_schema(tmp_path: Path) -> None:
    """Passing an invalid recipe (via mocking) should raise jsonschema.ValidationError."""
    import jsonschema

    recipe_dir = _scaffold_recipe_dir(tmp_path, n_cuts=0)
    # n_cuts=0 means cuts.json is empty list — schema requires minItems: 1
    (recipe_dir / "cuts.json").write_text(json.dumps([]))
    with pytest.raises(jsonschema.ValidationError):
        assemble_recipe.assemble("https://example.com/x", recipe_dir, REPO_ROOT)
