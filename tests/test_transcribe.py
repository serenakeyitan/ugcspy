"""Tests for scripts.transcribe — uses a fake whisper model so tests don't
download or run the real network of ML weights.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import transcribe


class FakeWhisperModel:
    """Whisper-API-compatible stand-in. Returns a canned result."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def transcribe(self, audio_path: str, **kwargs: Any) -> dict[str, Any]:
        return self._result


CANNED_RESULT = {
    "language": "en",
    "segments": [
        {
            "start": 0.0,
            "end": 2.0,
            "text": "Hello world",
            "words": [
                {"start": 0.0, "end": 0.5, "word": "Hello"},
                {"start": 0.6, "end": 1.0, "word": "world"},
            ],
        },
        {
            "start": 2.0,
            "end": 4.0,
            "text": "This is a test",
            "words": [
                {"start": 2.0, "end": 2.4, "word": "This"},
                {"start": 2.5, "end": 2.7, "word": "is"},
                {"start": 2.8, "end": 3.0, "word": "a"},
                {"start": 3.1, "end": 3.5, "word": "test"},
            ],
        },
    ],
}


def test_transcribe_writes_normalized_doc(tmp_path: Path) -> None:
    out = tmp_path / "transcript.json"
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")  # not actually used by fake
    doc = transcribe.transcribe_audio(audio, out, model=FakeWhisperModel(CANNED_RESULT))
    assert doc == json.loads(out.read_text())
    assert doc["language"] == "en"
    assert doc["duration_sec"] == 4.0
    assert len(doc["segments"]) == 2
    assert len(doc["words"]) == 6
    assert doc["words"][0]["word"] == "Hello"


def test_transcribe_handles_empty_segments(tmp_path: Path) -> None:
    out = tmp_path / "transcript.json"
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")
    doc = transcribe.transcribe_audio(
        audio, out, model=FakeWhisperModel({"language": "en", "segments": []})
    )
    assert doc["segments"] == []
    assert doc["words"] == []
    assert doc["duration_sec"] == 0.0


def test_pair_words_to_cuts_filters_by_time_range(tmp_path: Path) -> None:
    transcript = {
        "language": "en",
        "duration_sec": 4.0,
        "segments": [],
        "words": [
            {"start": 0.0, "end": 0.5, "word": "Hello"},
            {"start": 0.6, "end": 1.0, "word": "world"},
            {"start": 2.0, "end": 2.4, "word": "This"},
            {"start": 2.5, "end": 2.7, "word": "is"},
            {"start": 2.8, "end": 3.0, "word": "a"},
            {"start": 3.1, "end": 3.5, "word": "test"},
        ],
    }
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 2.0, "duration_sec": 2.0, "flagged_short": False},
        {"index": 1, "start_sec": 2.0, "end_sec": 4.0, "duration_sec": 2.0, "flagged_short": False},
    ]
    cuts_dir = tmp_path / "cuts"
    out_paths = transcribe.pair_words_to_cuts(transcript, cuts, cuts_dir)
    assert set(out_paths.keys()) == {0, 1}

    cut0 = json.loads(out_paths[0].read_text())
    assert cut0["text"] == "Hello world"
    assert len(cut0["words"]) == 2

    cut1 = json.loads(out_paths[1].read_text())
    assert cut1["text"] == "This is a test"
    assert len(cut1["words"]) == 4


def test_pair_words_to_cuts_handles_silent_cut(tmp_path: Path) -> None:
    """A cut with no spoken words should still get a transcript.json with empty text."""
    transcript = {
        "language": "en",
        "duration_sec": 4.0,
        "words": [{"start": 3.0, "end": 3.5, "word": "late"}],
    }
    cuts = [
        {"index": 0, "start_sec": 0.0, "end_sec": 2.0, "duration_sec": 2.0, "flagged_short": False},
    ]
    out_paths = transcribe.pair_words_to_cuts(transcript, cuts, tmp_path / "cuts")
    cut0 = json.loads(out_paths[0].read_text())
    assert cut0["text"] == ""
    assert cut0["words"] == []


def test_main_surfaces_ssl_self_help(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When transcribe_audio raises an SSL cert error, main() prints the
    certifi remediation and returns 1 — doesn't propagate the raw traceback."""
    import ssl

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")
    out_path = tmp_path / "transcript.json"

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )

    monkeypatch.setattr(transcribe, "transcribe_audio", boom)
    rc = transcribe.main([str(audio), str(out_path)])
    assert rc == 1


def test_main_re_raises_unrelated_exceptions(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Non-SSL failures should still propagate normally — we don't swallow them."""
    import pytest

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")
    out_path = tmp_path / "transcript.json"

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("genuinely unrelated bug")

    monkeypatch.setattr(transcribe, "transcribe_audio", boom)
    with pytest.raises(ValueError, match="genuinely unrelated"):
        transcribe.main([str(audio), str(out_path)])
