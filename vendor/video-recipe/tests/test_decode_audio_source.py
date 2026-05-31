"""Tests for decode.obtain_audio_transcript — the source-priority logic
that prefers the platform's embedded caption track over Whisper.

No network, no Whisper: we monkeypatch fetch_embedded_subs and
transcribe_source_audio to assert the priority + fallback wiring.
"""

from __future__ import annotations

from pathlib import Path

from scripts import decode


def _write_vtt(p: Path) -> Path:
    p.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.000\nhello from captions\n\n"
        "00:00:01.000 --> 00:00:02.000\nsecond cue\n"
    )
    return p


def test_prefers_embedded_subs_over_whisper(tmp_path, monkeypatch):
    """When the platform serves captions, use them and DON'T call Whisper."""
    recipe_dir = tmp_path / "rec"
    recipe_dir.mkdir()
    vtt = _write_vtt(recipe_dir / "embedded.eng-US.vtt")

    monkeypatch.setattr(
        decode, "transcribe_source_audio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Whisper should not run when captions exist")),
    )
    # Patch the symbol where obtain_audio_transcript imports it from.
    import scripts.embedded_subs as es
    monkeypatch.setattr(es, "fetch_embedded_subs", lambda url, dest, **k: (vtt, "eng-US"))

    out = decode.obtain_audio_transcript(
        "https://www.tiktok.com/@x/video/123", recipe_dir / "source.mp4", recipe_dir
    )
    assert out is not None
    assert out["source"] == "embedded_subs"
    assert out["language"] == "en"
    assert out["full_text"] == "hello from captions second cue"
    assert out["has_speech"] is True
    # transcript.json was persisted for downstream stages.
    assert (recipe_dir / "transcript.json").exists()


def test_falls_back_to_whisper_when_no_captions(tmp_path, monkeypatch):
    """No caption track → Whisper fallback is invoked and its result used."""
    recipe_dir = tmp_path / "rec"
    recipe_dir.mkdir()

    import scripts.embedded_subs as es
    monkeypatch.setattr(es, "fetch_embedded_subs", lambda url, dest, **k: None)

    sentinel = {"source": "whisper", "full_text": "from whisper", "has_speech": True}
    called = {}

    def fake_whisper(mp4, rd, model_name="base"):
        called["yes"] = True
        return sentinel

    monkeypatch.setattr(decode, "transcribe_source_audio", fake_whisper)

    out = decode.obtain_audio_transcript(
        "https://www.tiktok.com/@x/video/123", recipe_dir / "source.mp4", recipe_dir
    )
    assert called.get("yes") is True
    assert out is sentinel


def test_falls_back_to_whisper_when_no_url(tmp_path, monkeypatch):
    """No URL (decoding a bare recipe dir) → skip captions, go to Whisper."""
    recipe_dir = tmp_path / "rec"
    recipe_dir.mkdir()

    monkeypatch.setattr(
        decode, "transcribe_source_audio",
        lambda *a, **k: {"source": "whisper", "full_text": "w", "has_speech": True},
    )
    out = decode.obtain_audio_transcript(None, recipe_dir / "source.mp4", recipe_dir)
    assert out["source"] == "whisper"


def test_doc_to_audio_transcript_excludes_non_speech(tmp_path):
    """The flattener builds full_text from speech segments only — music +
    non-lexical segments stay in `segments` but never enter full_text."""
    doc = {
        "language": "en",
        "duration_sec": 6.0,
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "real line", "kind": "speech"},
            {"start": 2.0, "end": 4.0, "text": "", "kind": "non_speech"},
            {"start": 4.0, "end": 5.0, "text": "Uh", "kind": "non_lexical"},
        ],
        "words": [{"start": 0.0, "end": 0.5, "word": "real"}],
        "source": "whisper",
        "audio_kind": "mixed",
        "has_speech": True,
    }
    out = decode._doc_to_audio_transcript(doc, model_name="base")
    assert out["full_text"] == "real line"
    assert out["audio_kind"] == "mixed"
    assert len(out["segments"]) == 3  # all preserved
    assert out["model"] == "base"
