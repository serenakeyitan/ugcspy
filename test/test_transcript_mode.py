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


def test_cjk_speech_counts_characters_not_space_tokens():
    """A narrated Mandarin video has no spaces — text.split() would call a whole
    sentence ONE word and --talking would wrongly exclude it (Pingo AI's
    Chinese-tutor creators are exactly this case)."""
    result = {
        "language": "zh",
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "心理学表明你最喜欢的颜色揭示你的性格", "no_speech_prob": 0.05},
        ],
    }
    doc = tf._transcript_normalize(result)
    assert doc["audio_kind"] == "speech"
    # 18 CJK chars — far above the 8-word talking gate, as it should be.
    assert doc["lexical_word_count"] == 18


def test_mixed_cjk_and_latin_counts_both():
    # 我用学习 = 4 CJK chars; "BeFreed", "every", "day" = 3 latin tokens → 7
    assert tf._lexical_word_count("我用 BeFreed 学习 every day") == 7


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

class _FakeArr:
    """Minimal stand-in for the np array _load_audio_pcm builds — the test
    suite stays stdlib-only (CI runners have no numpy; the venv does)."""

    def flatten(self):
        return self

    def astype(self, _t):
        return self

    def __truediv__(self, _o):
        return self


def _install_fake_numpy(monkeypatch):
    import types

    fake_np = types.ModuleType("numpy")
    fake_np.frombuffer = lambda _b, _t: _FakeArr()
    fake_np.int16 = "int16"
    fake_np.float32 = "float32"
    monkeypatch.setitem(sys.modules, "numpy", fake_np)


def _install_fake_whisper(monkeypatch, result=None):
    import types

    fake_whisper = types.ModuleType("whisper")

    class FakeModel:
        def transcribe(self, _audio):
            return result if result is not None else SPEECH_RESULT

    fake_whisper.load_model = lambda name: FakeModel()
    monkeypatch.setitem(sys.modules, "whisper", fake_whisper)


def _run_transcript_expect_error(capsys, url, match):
    with pytest.raises(SystemExit) as exc:
        tf.run_transcript([url], batch=False)
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)
    assert match in err["error"]


def test_non_http_url_is_rejected(capsys, monkeypatch):
    import shutil

    _install_fake_whisper(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    _run_transcript_expect_error(capsys, "file:///etc/passwd", "must be http(s)")


def test_missing_whisper_yields_actionable_install_hint(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "whisper", None)  # import whisper → ImportError
    _run_transcript_expect_error(capsys, "https://www.tiktok.com/@x/video/1", "--with-audio")


def test_missing_ffmpeg_yields_actionable_hint(capsys, monkeypatch):
    """No system ffmpeg AND no imageio_ffmpeg (the bare-CI case for an old
    --with-audio install) → the error points at the self-contained re-install."""
    import shutil

    _install_fake_whisper(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)  # import → ImportError
    _run_transcript_expect_error(capsys, "https://www.tiktok.com/@x/video/1", "ffmpeg")


def test_ffmpeg_falls_back_to_bundled_imageio_binary(monkeypatch):
    import shutil
    import types

    monkeypatch.setattr(shutil, "which", lambda _: None)
    fake_imageio = types.ModuleType("imageio_ffmpeg")
    fake_imageio.get_ffmpeg_exe = lambda: "/venv/binaries/ffmpeg-macos-aarch64-v7.1"
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake_imageio)
    assert tf._resolve_ffmpeg() == "/venv/binaries/ffmpeg-macos-aarch64-v7.1"


def test_ffmpeg_prefers_system_binary(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: "/opt/homebrew/bin/ffmpeg")
    assert tf._resolve_ffmpeg() == "/opt/homebrew/bin/ffmpeg"


def test_ytdlp_failure_surfaces_stderr_tail(capsys, monkeypatch):
    import shutil
    import subprocess
    import types

    _install_fake_whisper(monkeypatch)
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


def _install_fake_pipeline(monkeypatch, tmp_path=None):
    """Stub the whole external surface (whisper, ffmpeg, numpy, subprocess) so
    run_transcript exercises the real control flow with no system deps."""
    import shutil
    import subprocess
    import types

    _install_fake_whisper(monkeypatch)
    _install_fake_numpy(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        if "-o" in cmd:  # yt-dlp download call — create the audio file
            out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a")
            Path(out).write_bytes(b"x")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        # ffmpeg PCM decode call — bytes out (capture_output, no encoding)
        return types.SimpleNamespace(returncode=0, stdout=b"\x00\x00", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_full_transcript_happy_path_with_stubbed_whisper(capsys, monkeypatch):
    """End-to-end single-url run_transcript with the pipeline stubbed: emits
    ONE JSON doc with audio_kind + lexical_word_count + the url echoed back."""
    _install_fake_pipeline(monkeypatch)
    tf.run_transcript(["https://www.tiktok.com/@x/video/1"], batch=False)
    doc = json.loads(capsys.readouterr().out)
    assert doc["audio_kind"] == "speech"
    assert doc["lexical_word_count"] == 8
    assert doc["video_url"] == "https://www.tiktok.com/@x/video/1"
    assert doc["whisper_model"] == "base"


def test_batch_returns_aligned_array_with_per_item_error_isolation(capsys, monkeypatch):
    """Batch form: ONE model load, array output aligned with input order, and a
    bad url yields an {error} element without sinking the good ones."""
    _install_fake_pipeline(monkeypatch)
    tf.run_transcript(
        [
            "https://www.tiktok.com/@a/video/1",
            "ftp://bad-scheme",
            "https://www.tiktok.com/@b/video/2",
        ],
        batch=True,
    )
    out = capsys.readouterr()
    docs = json.loads(out.out)
    assert len(docs) == 3
    assert docs[0]["video_url"] == "https://www.tiktok.com/@a/video/1"
    assert "error" in docs[1] and "http(s)" in docs[1]["error"]
    assert docs[2]["video_url"] == "https://www.tiktok.com/@b/video/2"
    # stderr progress lines, one per item
    assert "transcript 3/3 done" in out.err


def test_batch_with_all_failures_exits_nonzero_but_still_emits_the_array(capsys, monkeypatch):
    _install_fake_pipeline(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        tf.run_transcript(["ftp://a", "ftp://b"], batch=True)
    assert exc.value.code == 1
    docs = json.loads(capsys.readouterr().out)
    assert len(docs) == 2 and all("error" in d for d in docs)


def test_batch_keeps_blank_urls_aligned(capsys, monkeypatch):
    """A blank element must yield a per-item error envelope at ITS index —
    dropping it would shift the array and the caller would discard the whole
    wave as misaligned."""
    _install_fake_pipeline(monkeypatch)
    import io

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"mode": "transcript", "urls": ["https://www.tiktok.com/@a/video/1", "  ", "https://www.tiktok.com/@b/video/2"]})),
    )
    tf.main()
    docs = json.loads(capsys.readouterr().out)
    assert len(docs) == 3
    assert docs[0]["video_url"] == "https://www.tiktok.com/@a/video/1"
    assert "error" in docs[1]
    assert docs[2]["video_url"] == "https://www.tiktok.com/@b/video/2"


def test_dispatch_batch_urls(capsys, monkeypatch):
    import io

    _install_fake_pipeline(monkeypatch)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"mode": "transcript", "urls": ["https://www.tiktok.com/@a/video/1"]})),
    )
    tf.main()
    docs = json.loads(capsys.readouterr().out)
    assert isinstance(docs, list) and docs[0]["audio_kind"] == "speech"


def test_ytdlp_bin_resolves_windows_exe_next_to_interpreter(monkeypatch, tmp_path):
    """The bridge runs under the venv python WITHOUT venv activation, so the
    venv's Scripts dir is not on PATH — a Windows venv's yt-dlp.exe must be
    found beside sys.executable, not just the extensionless POSIX name."""
    exe = tmp_path / "Scripts" / "python.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    (tmp_path / "Scripts" / "yt-dlp.exe").write_bytes(b"")
    monkeypatch.setattr(tf.sys, "executable", str(exe))
    assert tf._ytdlp_bin().endswith("yt-dlp.exe")


def test_ytdlp_bin_falls_back_to_path_when_venv_has_none(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "python"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(tf.sys, "executable", str(exe))
    assert tf._ytdlp_bin() == "yt-dlp"


def test_dispatch_requires_url(capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"mode": "transcript"})))
    with pytest.raises(SystemExit):
        tf.main()
    assert "missing url" in json.loads(capsys.readouterr().out)["error"]


# ── prefetch pipeline: downloads overlap transcription, output stays in order ──


def test_prefetch_preserves_input_order_even_when_downloads_finish_out_of_order(
    capsys, monkeypatch
):
    """The batch loop prefetches audio downloads concurrently while transcribing
    serially in input order. Even if a LATER url's download finishes FIRST, the
    output array must stay aligned to the input order and each doc paired with
    its own url — that's what makes the optimization byte-identical to the old
    serial loop. We force reverse-order download completion to prove it."""
    import shutil
    import time

    _install_fake_whisper(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")

    urls = [f"https://www.tiktok.com/@x/video/{i}" for i in range(5)]

    # Fetch stage: later urls "download" faster, so completion order is reversed
    # vs submission order. Returns a marker dict carrying its url; no real I/O.
    def fake_fetch(url):
        idx = int(url.rsplit("/", 1)[1])
        time.sleep(0.02 * (len(urls) - idx))  # url 0 slowest, url 4 fastest
        return {"tmpdir": f"/tmp/fake-{idx}", "audio_path": f"/tmp/fake-{idx}/a.mp3", "url": url}

    # Transcribe stage: echo the url into the doc so we can check pairing; the
    # real cleanup is skipped (fake tmpdir).
    def fake_transcribe(fetched, _model, _name, _ffmpeg):
        if "error" in fetched:
            return {"error": fetched["error"], "video_url": fetched.get("video_url", "")}
        return {"video_url": fetched["url"], "transcript": f"tx for {fetched['url']}"}

    monkeypatch.setattr(tf, "_transcript_fetch_audio", fake_fetch)
    monkeypatch.setattr(tf, "_transcribe_fetched", fake_transcribe)

    tf.run_transcript(urls, batch=True)
    out = json.loads(capsys.readouterr().out)

    # Output order matches INPUT order (not download-completion order)...
    assert [d["video_url"] for d in out] == urls
    # ...and each doc carries ITS OWN url's transcript (no cross-pairing).
    for d in out:
        assert d["transcript"] == f"tx for {d['video_url']}"
