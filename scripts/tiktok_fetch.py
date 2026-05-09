#!/usr/bin/env python3
"""Bridge between ugcspy CLI and davidteather/TikTok-Api.

Stdin: JSON
  Handle mode:  { "mode": "user",    "handle":  "@befreed", "days": 30 }
  Hashtag mode: { "mode": "hashtag", "tag":     "befreed",  "days": 30 }
  (Legacy:      { "handle": "@x",    "days": 30 } is treated as user mode.)

Stdout (success): JSON array of RawVideo objects (matching src/types.ts).
Stdout (failure): JSON object { "error": "..." } and non-zero exit.

Requires:
  pip install -r scripts/requirements.txt
  python3 -m playwright install chromium
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional


def fail(msg: str, code: int = 1) -> None:
    print(json.dumps({"error": msg}))
    sys.exit(code)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        fail(f"invalid stdin json: {e}")

    mode = payload.get("mode") or ("user" if "handle" in payload else None)
    days = int(payload.get("days", 30))
    if mode is None:
        fail("missing mode (user|hashtag)")

    try:
        from TikTokApi import TikTokApi  # noqa: F401
    except ImportError:
        fail(
            "TikTokApi not installed. Run: pip install TikTokApi && python3 -m playwright install chromium"
        )

    if mode == "user":
        handle = (payload.get("handle") or "").lstrip("@")
        if not handle:
            fail("missing handle")
        asyncio.run(run_user(handle, days))
    elif mode == "hashtag":
        tag = (payload.get("tag") or "").lstrip("#")
        if not tag:
            fail("missing tag")
        asyncio.run(run_hashtag(tag, days))
    else:
        fail(f"unknown mode: {mode}")


async def _create_api():
    """Bot-detection bypass: chromium + headless=False is the combo that works
    in May 2026 (verified empirically). Pure headless gets blocked. If the user
    sets MS_TOKEN env var, we use it for higher reliability."""
    from TikTokApi import TikTokApi
    ms_token = os.environ.get("MS_TOKEN")
    kwargs = {
        "num_sessions": 1,
        "sleep_after": 3,
        "browser": "chromium",
        "headless": False,
    }
    if ms_token:
        kwargs["ms_tokens"] = [ms_token]
    api = TikTokApi()
    await api.__aenter__()
    await api.create_sessions(**kwargs)
    return api


def _video_to_raw(d: dict, fallback_handle: Optional[str] = None) -> Optional[dict]:
    """Convert TikTokApi video.as_dict to our RawVideo shape. Returns None if
    the post is too old or malformed."""
    create_ts = d.get("createTime") or 0
    if not create_ts:
        return None
    posted_at = datetime.fromtimestamp(create_ts, tz=timezone.utc)
    stats = d.get("stats", {}) or {}
    video_id = d.get("id") or ""
    author = (d.get("author") or {}).get("uniqueId") or fallback_handle or ""
    if not video_id:
        return None
    return {
        "platform": "tiktok",
        "external_id": str(video_id),
        "posted_at": posted_at.isoformat(),
        "caption": (d.get("desc") or "")[:1000],
        "thumbnail_url": (d.get("video", {}) or {}).get("cover", ""),
        "video_url": f"https://www.tiktok.com/@{author}/video/{video_id}" if author else f"https://www.tiktok.com/video/{video_id}",
        "view_count": int(stats.get("playCount", 0) or 0),
        "like_count": int(stats.get("diggCount", 0) or 0),
        "comment_count": int(stats.get("commentCount", 0) or 0),
        "share_count": int(stats.get("shareCount", 0) or 0),
        "_author": author,  # used for downstream UI; stripped on serialization if not in RawVideo
    }


async def run_user(handle: str, days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    api = None
    try:
        api = await _create_api()
        user = api.user(handle)
        async for video in user.videos(count=60):
            d = video.as_dict
            raw = _video_to_raw(d, fallback_handle=handle)
            if raw is None:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            raw.pop("_author", None)
            videos.append(raw)
    except Exception as e:
        fail(f"TikTokApi error (user): {e}", code=2)
    finally:
        if api is not None:
            try:
                await api.__aexit__(None, None, None)
            except Exception:
                pass

    print(json.dumps(videos))


async def run_hashtag(tag: str, days: int) -> None:
    """Fetch videos tagged with #tag posted by any creator. This is the
    third-party-UGC discovery path — finds creators promoting a brand, not
    the brand's own posts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    seen_ids: set[str] = set()
    api = None
    try:
        api = await _create_api()
        hashtag = api.hashtag(name=tag)
        async for video in hashtag.videos(count=60):
            d = video.as_dict
            raw = _video_to_raw(d)
            if raw is None or raw["external_id"] in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            seen_ids.add(raw["external_id"])
            # Keep _author so the caller can show "@creator: caption" in the UI.
            videos.append(raw)
    except Exception as e:
        fail(f"TikTokApi error (hashtag): {e}", code=2)
    finally:
        if api is not None:
            try:
                await api.__aexit__(None, None, None)
            except Exception:
                pass

    print(json.dumps(videos))


if __name__ == "__main__":
    main()
