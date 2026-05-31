"""Regression test for scripts.decode.ocr_all_frames.

Guards the fix for the UnicodeDecodeError crash: some tesseract builds emit
non-UTF-8 bytes on stderr. When ocr_all_frames ran tesseract with text=True,
subprocess tried to UTF-8-decode that stderr and raised
`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff`, killing the whole
decode mid-run (after download + frame extraction already succeeded). The OCR
result is only ever read from the .txt output file, so capturing stdout/stderr
as text was incidental. The fix drops text=True (capture raw bytes).

This test fakes subprocess.run to mimic that exact failure mode: if the caller
asks for text decoding (text=True / encoding=...), it reproduces the
UnicodeDecodeError the real subprocess would raise on 0xff stderr; otherwise it
writes the .txt sidecar and returns raw bytes. So the test fails loudly if
text=True is ever reintroduced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import decode

# A byte sequence that is invalid UTF-8 — exactly the kind of thing a
# non-UTF-8 tesseract build can splatter onto stderr.
_BAD_STDERR = b"\xff\xfe tesseract noise \x80\x81"


def _fake_tesseract_run(monkeypatch, frames_dir: Path) -> None:
    """Patch subprocess.run so 'tesseract <jpg> <out>' writes <out>.txt and
    behaves like a build that prints non-UTF-8 bytes on stderr."""

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        # Reproduce the original crash: if the caller forces text decoding,
        # the real subprocess would choke on the 0xff stderr bytes.
        if kwargs.get("text") or kwargs.get("encoding") is not None:
            raise UnicodeDecodeError(
                "utf-8", _BAD_STDERR, 0, 1, "invalid start byte"
            )
        # cmd == ["tesseract", <jpg>, <out_stem>, ...]; tesseract appends .txt.
        out_stem = Path(cmd[2])
        out_stem.with_suffix(".txt").write_text("HELLO WORLD\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=_BAD_STDERR)

    monkeypatch.setattr(decode.subprocess, "run", fake_run)


def test_ocr_all_frames_survives_non_utf8_tesseract_stderr(
    monkeypatch, tmp_path: Path
) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    # Two frames so we exercise the loop more than once.
    (frames_dir / "f-001.jpg").write_bytes(b"\x00")
    (frames_dir / "f-002.jpg").write_bytes(b"\x00")
    ocr_dir = tmp_path / "ocr"

    _fake_tesseract_run(monkeypatch, frames_dir)

    # Before the fix this raised UnicodeDecodeError; after it, it returns text.
    result = decode.ocr_all_frames(frames_dir, ocr_dir)

    assert result == {"f-001": "HELLO WORLD", "f-002": "HELLO WORLD"}
