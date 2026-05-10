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
    the brand's own posts.

    Coverage strategy (THREE-pass): TikTok's hashtag endpoint is capped
    at ~200 per tag and ranks by an opaque algo that aggressively
    DEDUPES per-creator (so a creator with 30 #befreed posts only shows
    1-2 in the hashtag feed). The fix is to seed creators from hashtags,
    then pull each seed creator's full feed and re-filter.

      Pass 1: hashtag fetch — primary tag + brand-app variant.
      Pass 2: discover campaign codes (#brand_NNNN) from pass-1 captions,
              fetch each one. (Most BeFreed UGC carries unique campaign
              codes per video, so this surfaces variety.)
      Pass 3: enumerate seed creators from passes 1-2 (anyone whose
              caption matched the precision filter, OR whose handle
              contains the brand). Fetch each seed creator's full recent
              feed. Re-apply caption filter.

    Result on BeFreed: pass 1+2 gave 71 real UGC posts. Pass 3 surfaces
    @annaa.learns's 334K-view post (#befreed_0085), @growthwithmya7's
    157K-view post (#befreed_0124), and ~150-300 more posts from prolific
    creators that the hashtag feed had de-ranked."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    seen_ids: set[str] = set()

    api = None
    try:
        api = await _create_api()

        # Pass 1: primary tag + brand-app variant
        pass_1_tags = [tag, f"{tag}app"]
        for variant in pass_1_tags:
            await _fetch_one_hashtag(api, variant, videos, seen_ids, cutoff)

        # Pass 2: discover campaign codes from pass-1 captions, query each
        codes = _discover_campaign_codes(videos, tag)
        for code in codes:
            variant = f"{tag}_{code}"
            await _fetch_one_hashtag(api, variant, videos, seen_ids, cutoff)

        # Pass 3: enumerate seed creators, fetch each one's full feed.
        # We cap at 15 seed creators to bound runtime — sorted by post
        # count in pass 1+2 so we hit the most-prolific ones first.
        seed_creators = _discover_seed_creators(videos, tag)
        for creator in seed_creators[:15]:
            await _fetch_one_creator(api, creator, videos, seen_ids, cutoff, tag)
    except Exception as e:
        fail(f"TikTokApi error (hashtag): {e}", code=2)
    finally:
        if api is not None:
            try:
                await api.__aexit__(None, None, None)
            except Exception:
                pass

    print(json.dumps(videos))


async def _fetch_one_hashtag(api, variant, videos, seen_ids, cutoff):
    """Fetch one hashtag's videos and merge into shared lists. Failures
    are swallowed per-variant — a missing tag or transient bot-detection
    blip shouldn't kill the whole search."""
    try:
        hashtag = api.hashtag(name=variant)
        async for video in hashtag.videos(count=200):
            d = video.as_dict
            raw = _video_to_raw(d)
            if raw is None or raw["external_id"] in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            seen_ids.add(raw["external_id"])
            videos.append(raw)
    except AttributeError as e:
        if "'Hashtag' object has no attribute 'id'" in str(e):
            return  # tag doesn't exist
        raise
    except Exception:
        return  # rate-limit, bot-detection, etc — skip this variant


def _discover_campaign_codes(videos, tag):
    """Extract campaign-code variants from caption text. If we see
    `#befreed_0117` mentioned in the captions of pass-1 results, that's
    a live campaign code worth querying directly for more coverage.

    Returns a sorted list of unique 4-digit codes (max 12 to bound runtime)."""
    import re
    code_pattern = re.compile(r"#" + re.escape(tag) + r"_(\d{2,4})\b", re.IGNORECASE)
    seen_codes = set()
    for v in videos:
        for match in code_pattern.finditer(v.get("caption") or ""):
            code = match.group(1).zfill(4)
            seen_codes.add(code)
    # Cap at 12 to bound wall time (each adds ~3-8s scrape)
    return sorted(seen_codes)[:12]


def _is_real_ugc_caption(caption, tag):
    """Mirror of TS isHashtagMatch — does this caption explicitly carry
    the brand hashtag/mention? Used to qualify seed creators."""
    import re
    if not caption:
        return False
    escaped = re.escape(tag.lstrip("@#"))
    pattern = re.compile(
        r"#" + escaped + r"(?![a-z0-9_])|"
        r"#" + escaped + r"_\d+|"
        r"#" + escaped + r"app(?![a-z0-9_])|"
        r"@" + escaped + r"(?![a-z0-9_])",
        re.IGNORECASE,
    )
    return bool(pattern.search(caption))


def _discover_seed_creators(videos, tag):
    """Identify creators worth pulling full feeds from. Two signals:
      1. Their handle contains the brand name (e.g. @laura.befreed,
         @eilisa.befreed) — these are dedicated UGC creators.
      2. They've posted at least 1 video that passes the precision
         filter — they've actually tagged this brand at least once.

    Returns creators sorted by post-count desc, deduped."""
    counts = {}
    handle_lower_brand = tag.lstrip("@#").lower()
    for v in videos:
        author = v.get("_author") or ""
        if not author:
            continue
        # Signal 1: handle contains brand name
        handle_signal = handle_lower_brand in author.lower()
        # Signal 2: caption passes the filter
        caption_signal = _is_real_ugc_caption(v.get("caption") or "", tag)
        if handle_signal or caption_signal:
            counts[author] = counts.get(author, 0) + 1
    # Sort by post count desc — we want to spend our 15-creator budget on
    # the prolific ones, not one-off matches
    return sorted(counts.keys(), key=lambda h: -counts[h])


async def _fetch_one_creator(api, handle, videos, seen_ids, cutoff, tag):
    """Pull a creator's recent feed and merge any posts that pass the
    precision filter. Per-creator failures are swallowed — one bad
    handle shouldn't kill the whole pass."""
    try:
        user = api.user(handle)
        async for video in user.videos(count=50):
            d = video.as_dict
            raw = _video_to_raw(d, fallback_handle=handle)
            if raw is None or raw["external_id"] in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            # Apply precision filter at fetch time so we don't pollute
            # videos[] with off-brand posts the creator made.
            if not _is_real_ugc_caption(raw.get("caption") or "", tag):
                continue
            seen_ids.add(raw["external_id"])
            videos.append(raw)
    except AttributeError as e:
        if "no attribute 'id'" in str(e):
            return
        raise
    except Exception:
        return


if __name__ == "__main__":
    main()
