"""Tests for the bridge's transcript mode (scripts/tiktok_fetch.py).

Includes the PARITY GUARD: _transcript_normalize is a deliberate copy of
vendor/video-recipe/scripts/transcribe.py::_result_to_doc (the npm tarball
ships scripts/ but not vendor/, so the bridge can't import across). These
tests feed the same fixtures to BOTH implementations and assert identical
output on the shared keys — change one, and this fails until you change both.

No network, no whisper, no venv: stdlib + pytest. Run with:
    python3 -m pytest test/test_transcript_mode.py
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tiktok_fetch as tf  # noqa: E402

VENDOR_TRANSCRIBE = (
    Path(__file__).resolve().parent.parent / "vendor" / "video-recipe" / "scripts" / "transcribe.py"
)


def _load_vendor_transcribe():
    import importlib.util

    spec = importlib.util.spec_from_file_location("vendor_transcribe", VENDOR_TRANSCRIBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fixtures ──────────────────────────────────────────────────────────────────

SPEECH_RESULT = {
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 4.1, "text": " Here is the hook. ", "no_speech_prob": 0.02},
        {"start": 4.1, "end": 9.0, "text": "More narration follows here.", "no_speech_prob": 0.1},
    ],
}

# Whisper hallucinating lyrics over a music bed — text must be BLANKED.
MUSIC_RESULT = {
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 15.0, "text": "ooh baby tonight we fly", "no_speech_prob": 0.91},
        {"start": 15.0, "end": 30.0, "text": "fly away with me", "no_speech_prob": 0.85},
    ],
}

MIXED_RESULT = {
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 5.0, "text": "Real spoken intro line.", "no_speech_prob": 0.05},
        {"start": 5.0, "end": 20.0, "text": "la la la", "no_speech_prob": 0.8},
        {"start": 20.0, "end": 22.0, "text": "Mmm", "no_speech_prob": 0.2},
    ],
}


# ── parity guard vs vendor ────────────────────────────────────────────────────

@pytest.mark.parametrize("fixture", [SPEECH_RESULT, MUSIC_RESULT, MIXED_RESULT])
def test_parity_with_vendor_result_to_doc(fixture):
    vendor = _load_vendor_transcribe()
    ours = tf._transcript_normalize(fixture)
    theirs = vendor._result_to_doc(fixture)
    for key in ("language", "duration_sec", "segments", "words", "audio_kind"):
        assert ours[key] == theirs[key], f"parity break on {key!r}"


def test_parity_threshold_constants_match():
    vendor = _load_vendor_transcribe()
    assert tf.TRANSCRIPT_NO_SPEECH_PROB == vendor.NO_SPEECH_PROB_THRESHOLD


# ── normalize behavior ────────────────────────────────────────────────────────

def test_music_bed_lyrics_are_blanked_and_kind_is_music():
    doc = tf._transcript_normalize(MUSIC_RESULT)
    assert doc["audio_kind"] == "music"
    assert all(seg["text"] == "" for seg in doc["segments"])
    assert doc["lexical_word_count"] == 0


def test_speech_kind_and_lexical_word_count():
    doc = tf._transcript_normalize(SPEECH_RESULT)
    assert doc["audio_kind"] == "speech"
    # "Here is the hook." (4) + "More narration follows here." (4)
    assert doc["lexical_word_count"] == 8


def test_mixed_kind_counts_only_speech_words():
    doc = tf._transcript_normalize(MIXED_RESULT)
    assert doc["audio_kind"] == "mixed"
    assert doc["lexical_word_count"] == 4  # only "Real spoken intro line."
    kinds = [s["kind"] for s in doc["segments"]]
    assert kinds == ["speech", "non_speech", "non_lexical"]


def test_garbage_segments_are_dropped_not_crashed_on():
    doc = tf._transcript_normalize(
        {"language": "en", "segments": [None, 42, {"start": "x", "end": "y"}, *SPEECH_RESULT["segments"]]}
    )
    assert doc["audio_kind"] == "speech"
    assert len(doc["segments"]) == 2


def test_empty_result_is_speech_kind_with_no_segments():
    # No segments at all (silent clip): vendor rule = "speech" (no nonspeech
    # segments seen), zero words — isTalking() downstream still rejects it on
    # the word-count gate.
    doc = tf._transcript_normalize({})
    assert doc["segments"] == []
    assert doc["lexical_word_count"] == 0


# ── run_transcript error contract ─────────────────────────────────────────────

def _run_transcript_expect_error(capsys, url, match):
    with pytest.raises(SystemExit) as exc:
        tf.run_transcript(url)
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)
    assert match in err["error"]


def test_non_http_url_is_rejected(capsys):
    _run_transcript_expect_error(capsys, "file:///etc/passwd", "must be http(s)")


def test_missing_whisper_yields_actionable_install_hint(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "whisper", None)  # import whisper → ImportError
    _run_transcript_expect_error(capsys, "https://www.tiktok.com/@x/video/1", "--with-audio")


def test_missing_ffmpeg_yields_actionable_hint(capsys, monkeypatch):
    import shutil
    import types

    monkeypatch.setitem(sys.modules, "whisper", types.ModuleType("whisper"))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    _run_transcript_expect_error(capsys, "https://www.tiktok.com/@x/video/1", "ffmpeg")


def test_ytdlp_failure_surfaces_stderr_tail(capsys, monkeypatch):
    import shutil
    import subprocess
    import types

    monkeypatch.setitem(sys.modules, "whisper", types.ModuleType("whisper"))
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Video unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_transcript_expect_error(capsys, "https://www.tiktok.com/@x/video/1", "Video unavailable")


def test_download_audio_passes_url_after_double_dash(monkeypatch, tmp_path):
    """A crafted URL can't smuggle yt-dlp flags — argv must carry `--` before it."""
    import subprocess
    import types

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        # Satisfy the post-run glob check.
        (tmp_path / "audio.m4a").write_bytes(b"x")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    path = tf._transcript_download_audio("https://t/--simulate", str(tmp_path))
    assert path.endswith("audio.m4a")
    dd = seen["cmd"].index("--")
    assert seen["cmd"][dd + 1] == "https://t/--simulate"


def test_oversized_audio_is_rejected(monkeypatch, tmp_path):
    import subprocess
    import types

    def fake_run(cmd, **kwargs):
        (tmp_path / "audio.m4a").write_bytes(b"x")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(tf.os.path, "getsize", lambda _: tf.TRANSCRIPT_MAX_AUDIO_BYTES + 1)
    with pytest.raises(RuntimeError, match="exceeds"):
        tf._transcript_download_audio("https://t/v", str(tmp_path))


def test_full_transcript_happy_path_with_stubbed_whisper(capsys, monkeypatch, tmp_path):
    """End-to-end run_transcript with whisper + yt-dlp stubbed: emits ONE JSON
    doc with audio_kind + lexical_word_count + the url echoed back."""
    import shutil
    import subprocess
    import types

    fake_whisper = types.ModuleType("whisper")

    class FakeModel:
        def transcribe(self, path):
            return SPEECH_RESULT

    fake_whisper.load_model = lambda name: FakeModel()
    monkeypatch.setitem(sys.modules, "whisper", fake_whisper)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a")
        Path(out).write_bytes(b"x")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    tf.run_transcript("https://www.tiktok.com/@x/video/1")
    doc = json.loads(capsys.readouterr().out)
    assert doc["audio_kind"] == "speech"
    assert doc["lexical_word_count"] == 8
    assert doc["video_url"] == "https://www.tiktok.com/@x/video/1"
    assert doc["whisper_model"] == "base"


def test_dispatch_requires_url(capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"mode": "transcript"})))
    with pytest.raises(SystemExit):
        tf.main()
    assert "missing url" in json.loads(capsys.readouterr().out)["error"]
