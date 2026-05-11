"""Unit tests for scripts.download — no real network calls."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import download


def test_resolve_video_id_prefers_yt_dlp_id() -> None:
    info = {"id": "abc123", "title": "x"}
    assert download.resolve_video_id(info, "https://example.com/x") == "abc123"


def test_resolve_video_id_falls_back_to_url_hash() -> None:
    vid = download.resolve_video_id(None, "https://example.com/x")
    assert len(vid) == 12
    # deterministic
    assert vid == download.resolve_video_id({}, "https://example.com/x")


def _install_fake_yt_dlp(
    monkeypatch: pytest.MonkeyPatch,
    info: dict,
    *,
    write_video: bool = True,
    raise_on: str | None = None,
) -> MagicMock:
    """Install a fake yt_dlp module that records calls and optionally writes a fake mp4."""

    class FakeError(Exception):
        pass

    captured = MagicMock()

    class FakeYDL:
        def __init__(self, opts):
            captured.last_opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if raise_on == ("probe" if not download else "download"):
                raise FakeError("simulated")
            if download and write_video:
                out = Path(captured.last_opts["outtmpl"])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"fake mp4 bytes")
            return info

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL  # type: ignore[attr-defined]
    fake.utils = types.SimpleNamespace(DownloadError=FakeError)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    return captured


def test_download_writes_video_and_info(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    info = {
        "id": "vid42",
        "title": "Example",
        "duration": 12.5,
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "ext": "mp4",
        "extractor": "youtube",
        "webpage_url": "https://example.com/v/42",
    }
    _install_fake_yt_dlp(monkeypatch, info)

    video_id, recipe_dir = download.download("https://example.com/v/42", tmp_path)

    assert video_id == "vid42"
    assert recipe_dir == tmp_path / "vid42"
    assert (recipe_dir / "source.mp4").exists()
    assert (recipe_dir / "source.mp4").stat().st_size > 0

    info_data = (recipe_dir / "source.info.json").read_text()
    assert "vid42" in info_data
    assert "1920" in info_data


def test_download_raises_clear_error_when_yt_dlp_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_yt_dlp(monkeypatch, {"id": "x"}, raise_on="probe")
    with pytest.raises(RuntimeError, match="could not resolve"):
        download.download("https://example.com/dead", tmp_path)


def test_download_raises_when_file_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_yt_dlp(monkeypatch, {"id": "empty"}, write_video=False)
    with pytest.raises(RuntimeError, match="empty file"):
        download.download("https://example.com/empty", tmp_path)


def test_main_surfaces_ssl_self_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When download() raises a wrapped SSL cert error, main() prints the
    certifi remediation and returns 1."""

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Match the shape yt-dlp produces: a RuntimeError wrapping the SSL
        # error in __cause__.
        outer = RuntimeError("yt-dlp could not resolve URL")
        outer.__cause__ = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        raise outer

    monkeypatch.setattr(download, "download", boom)
    rc = download.main(["https://example.com/v/x", "--recipes-root", str(tmp_path)])
    assert rc == 1


def test_main_re_raises_non_ssl_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("not an SSL problem")

    monkeypatch.setattr(download, "download", boom)
    with pytest.raises(ValueError, match="not an SSL problem"):
        download.main(["https://example.com/x", "--recipes-root", str(tmp_path)])
