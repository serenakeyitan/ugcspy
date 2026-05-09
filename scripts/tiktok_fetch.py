#!/usr/bin/env python3
"""Bridge between ugcspy CLI and davidteather/TikTok-Api.

Stdin: JSON { "handle": "@glossier", "days": 30 }
Stdout (success): JSON array of RawVideo objects (matching src/types.ts).
Stdout (failure): JSON object { "error": "..." } and non-zero exit.

Requires:
  pip install TikTokApi
  python3 -m playwright install chromium
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone


def fail(msg: str, code: int = 1) -> None:
    print(json.dumps({"error": msg}))
    sys.exit(code)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        fail(f"invalid stdin json: {e}")

    handle = payload.get("handle", "").lstrip("@")
    days = int(payload.get("days", 30))
    if not handle:
        fail("missing handle")

    try:
        from TikTokApi import TikTokApi  # type: ignore
    except ImportError:
        fail(
            "TikTokApi not installed. Run: pip install TikTokApi && python3 -m playwright install chromium"
        )

    asyncio.run(run(handle, days))


async def run(handle: str, days: int) -> None:
    from TikTokApi import TikTokApi  # type: ignore

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []

    # Bot-detection bypass: TikTok blocks headless and even webkit non-headless,
    # but `chromium` with `headless=False` works (verified empirically May 2026).
    # This means the user sees a brief Chromium window flash open during scrapes.
    # If MS_TOKEN env var is set, we use it for higher reliability — grab from
    # your browser's tiktok.com cookies (DevTools → Application → Cookies).
    import os as _os
    ms_token = _os.environ.get("MS_TOKEN")
    session_kwargs = {
        "num_sessions": 1,
        "sleep_after": 3,
        "browser": "chromium",
        "headless": False,
    }
    if ms_token:
        session_kwargs["ms_tokens"] = [ms_token]

    try:
        async with TikTokApi() as api:
            await api.create_sessions(**session_kwargs)
            user = api.user(handle)
            async for video in user.videos(count=60):
                d = video.as_dict
                create_ts = d.get("createTime") or 0
                posted_at = datetime.fromtimestamp(create_ts, tz=timezone.utc) if create_ts else None
                if posted_at is None or posted_at < cutoff:
                    continue
                stats = d.get("stats", {}) or {}
                video_id = d.get("id") or ""
                videos.append(
                    {
                        "platform": "tiktok",
                        "external_id": str(video_id),
                        "posted_at": posted_at.isoformat(),
                        "caption": (d.get("desc") or "")[:1000],
                        "thumbnail_url": (d.get("video", {}) or {}).get("cover", ""),
                        "video_url": f"https://www.tiktok.com/@{handle}/video/{video_id}",
                        "view_count": int(stats.get("playCount", 0) or 0),
                        "like_count": int(stats.get("diggCount", 0) or 0),
                        "comment_count": int(stats.get("commentCount", 0) or 0),
                        "share_count": int(stats.get("shareCount", 0) or 0),
                    }
                )
    except Exception as e:
        fail(f"TikTokApi error: {e}", code=2)

    print(json.dumps(videos))


if __name__ == "__main__":
    main()
