"""Download a video by URL into a recipe directory.

Resolves a deterministic video_id from the URL (yt-dlp's id when available,
otherwise a sha1 of the URL), creates ``recipes/<video_id>/`` if needed,
and writes ``source.mp4`` plus ``source.info.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def resolve_video_id(info: dict[str, Any] | None, url: str) -> str:
    """Pick a deterministic id: prefer yt-dlp's id, fall back to sha1(url)[:12]."""
    if info and info.get("id"):
        return str(info["id"])
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def download(url: str, recipes_root: Path) -> tuple[str, Path]:
    """Download ``url`` into ``recipes_root/<video_id>/``.

    Returns (video_id, recipe_dir). Raises RuntimeError on download failure.
    """
    # Lazy import so unit tests can stub the module without pulling yt-dlp.
    import yt_dlp

    # Probe metadata first to learn the id, so we can name the directory.
    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"yt-dlp could not resolve {url!r}: {e}") from e

    video_id = resolve_video_id(info, url)
    recipe_dir = recipes_root / video_id
    recipe_dir.mkdir(parents=True, exist_ok=True)

    out_path = recipe_dir / "source.mp4"
    info_path = recipe_dir / "source.info.json"

    # Cap at 1080p, prefer mp4 container for downstream ffmpeg compatibility.
    download_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(out_path),
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]",
        "merge_output_format": "mp4",
        "noprogress": True,
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            full_info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"yt-dlp failed to download {url!r}: {e}") from e

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Download produced empty file at {out_path}")

    # Strip large fields we don't need; keep what's useful for later stages.
    keep = {
        "id",
        "title",
        "duration",
        "fps",
        "width",
        "height",
        "ext",
        "vcodec",
        "acodec",
        "uploader",
        "uploader_id",
        "webpage_url",
        "extractor",
    }
    slim_info = {k: full_info.get(k) for k in keep if k in full_info}
    info_path.write_text(json.dumps(slim_info, indent=2, sort_keys=True))

    return video_id, recipe_dir


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Download a video by URL.")
    parser.add_argument("url")
    parser.add_argument(
        "--recipes-root",
        type=Path,
        default=Path("recipes"),
        help="Where recipe directories live (default: recipes/)",
    )
    args = parser.parse_args(argv)
    from scripts._log import (
        get_logger,
        is_ssl_certificate_error,
        print_ssl_self_help,
        stage,
    )

    try:
        with stage("download"):
            video_id, recipe_dir = download(args.url, args.recipes_root)
    except Exception as exc:
        if is_ssl_certificate_error(exc):
            print_ssl_self_help(get_logger("download"))
            return 1
        raise
    print(f"{video_id}\t{recipe_dir / 'source.mp4'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
