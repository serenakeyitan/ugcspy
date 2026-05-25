#!/usr/bin/env python3
"""Bridge between ugcspy CLI and davidteather/TikTok-Api.

Stdin: JSON
  Handle mode:  { "mode": "user",    "handle":  "@befreed", "days": 30 }
  Hashtag mode: { "mode": "hashtag", "tag":     "befreed",  "days": 30 }
  (Legacy:      { "handle": "@x",    "days": 30 } is treated as user mode.)

Stdout (success): JSON array of RawVideo objects (matching src/types.ts).
Stdout (failure): JSON object { "error": "..." } and non-zero exit.

Requires the managed ugcspy venv. Bootstrap with `ugcspy install-deps` (creates
~/.ugcspy/venv and installs TikTokApi + Chromium into it). Invoke via that venv's
python — the TypeScript provider does this; running this script under a different
interpreter will fail the import check below.
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
            "TikTokApi not installed in the active interpreter. Run `ugcspy install-deps` (creates a managed venv at ~/.ugcspy/venv)."
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

    Coverage strategy (FOUR-pass with bounded parallelism): TikTok's
    hashtag endpoint returns ~140-200 per call and dedupes aggressively
    per-creator. Single-hashtag is wildly incomplete; we work around it
    via four passes:

      Pass 0: user-search for handles matching the brand (`Search.users`).
              Surfaces dedicated UGC creators like @laura.befreed and the
              official account @befreedapp.
      Pass 1: hashtag fetch — primary tag + brand-app variant (parallel).
      Pass 2: discover campaign codes (#brand_NNNN) from pass-1 captions,
              fetch each one (parallel).
      Pass 3: enumerate seed creators from passes 0-2 + pull each one's
              full recent feed (parallel). Re-apply caption filter.

    Parallelization cap: 8 concurrent fetches. Verified empirically — 4
    sequential fetches took 21s, 4 parallel took 7s (~3x speedup); 8
    parallel took 7.2s with zero rate-limit errors. Going beyond 8 is
    untested and risks tripping TikTok's anti-abuse rules.

    Each task returns its own video list; merging happens serially after
    asyncio.gather to avoid race conditions on shared state.

    Result on BeFreed: ~440 videos, max 334K views. Wall time:
    ~95s sequential -> ~45s parallel (estimated)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    seen_ids: set[str] = set()

    api = None
    try:
        api = await _create_api()

        # Pass 0: user-search seeding (sequential — single fast call)
        user_search_seeds = await _user_search_seeds(api, tag)

        # Pass 1: primary tag + brand-app variant — fetched in parallel.
        # Each call returns its own list; merged serially below.
        pass_1_tags = [tag, f"{tag}app"]
        pass_1_results = await _gather_with_concurrency(
            _concurrency_limit(), [_fetch_hashtag_isolated(api, v, cutoff) for v in pass_1_tags]
        )
        _merge_into_videos(pass_1_results, videos, seen_ids)

        # Pass 2: discover campaign codes, fetch all in parallel
        codes = _discover_campaign_codes(videos, tag)
        if codes:
            pass_2_results = await _gather_with_concurrency(
                8, [_fetch_hashtag_isolated(api, f"{tag}_{c}", cutoff) for c in codes]
            )
            _merge_into_videos(pass_2_results, videos, seen_ids)

        # Pass 3: seed creators in parallel
        seed_creators = _merge_seed_creators(videos, tag, user_search_seeds)
        if seed_creators:
            pass_3_results = await _gather_with_concurrency(
                8, [_fetch_creator_isolated(api, c, cutoff, tag) for c in seed_creators[:25]]
            )
            _merge_into_videos(pass_3_results, videos, seen_ids)
    except Exception as e:
        fail(f"TikTokApi error (hashtag): {e}", code=2)
    finally:
        if api is not None:
            try:
                await api.__aexit__(None, None, None)
            except Exception:
                pass

    print(json.dumps(videos))


def _concurrency_limit():
    """Concurrency cap for parallel hashtag/creator fetches.

    Default is 12 — empirically validated against fresh-IP probes:

      Probe (16 hashtags, fresh IP, May 2026):
        c=4  -> 6.8s,  387 videos, 0 errors
        c=8  -> 11.4s, 1186 videos, 0 errors  <-- old default
        c=12 -> 7.0s,  1172 videos, 0 errors  <-- new default
        c=16 -> 7.0s,  1207 videos, 0 errors

      Full pipeline E2E:
        c=8  -> 67s, 410-440 videos
        c=12 -> 68s, 444 videos
        c=16 -> 62s, 444 videos

    The probe gain from 8 -> 12 is real (1.6x faster, same coverage).
    Past 12 the per-request latency dominates, so 16 is only ~10%
    faster end-to-end. We pick 12 as conservative enough to not risk
    rate-limiting on slower IPs while capturing the meaningful gain.

    Users with MS_TOKEN set (browser-cookie-authed sessions) have a
    higher rate-limit ceiling and may safely go to 16 or 24.
    Override via env: `UGCSPY_CONCURRENCY=16 ugcspy search ...`

    Cautionary tale (commit 2610607): pushing scraping aggressively
    AFTER an IP is already throttled returns ZERO videos for ~10-20
    minutes — not just "less, but slower". The 12 default has measured
    safety margin; don't bump it without fresh-IP probe data."""
    raw = os.environ.get("UGCSPY_CONCURRENCY", "")
    try:
        n = int(raw)
        if n >= 1:
            return n
    except (ValueError, TypeError):
        pass
    return 12


async def _gather_with_concurrency(limit, coros):
    """asyncio.gather but bounded — at most `limit` coroutines run
    concurrently. Prevents overwhelming TikTok with 25 parallel
    requests when seed_creators is full."""
    semaphore = asyncio.Semaphore(limit)
    async def bounded(coro):
        async with semaphore:
            return await coro
    return await asyncio.gather(*(bounded(c) for c in coros))


def _merge_into_videos(per_task_results, videos, seen_ids):
    """Serial merge of per-task results into the shared videos list,
    deduplicating by external_id. Runs after all parallel fetches
    complete, so no race conditions."""
    for task_videos in per_task_results:
        if not task_videos:
            continue
        for v in task_videos:
            ext = v.get("external_id")
            if not ext or ext in seen_ids:
                continue
            seen_ids.add(ext)
            videos.append(v)


async def _user_search_seeds(api, tag):
    """Use TikTok's user-search endpoint to find handles containing the
    brand name. Returns a list of usernames (no @). These are CANDIDATES
    — many will be noise (palestine_willbefreed, befreedwinefarm, etc.)
    that get filtered out in pass 3 when their captions don't match.

    We pull from two queries: `tag` and `tagapp`, since the official
    account often uses the latter (e.g. @befreedapp)."""
    seen = []
    seen_set = set()
    for query in [tag, f"{tag}app"]:
        try:
            count = 0
            async for u in api.search.users(query, count=30):
                username = getattr(u, "username", None)
                if username and username not in seen_set:
                    seen.append(username)
                    seen_set.add(username)
                count += 1
                if count >= 25:
                    break
        except Exception:
            continue
    return seen


def _merge_seed_creators(videos, tag, user_search_seeds):
    """Combine seed signals from all passes:
      A. Creators whose handle contains the brand name (strongest signal —
         these are dedicated UGC accounts like @laura.befreed)
      B. Creators who already have a caption that passed the filter (proven
         UGC for this brand)
      C. Usernames from pass-0 user search (may be noise but cheap to add)

    Returns deduped creators sorted: A+B first (strong signals), then C."""
    handle_lower_brand = tag.lstrip("@#").lower()

    counts = {}
    handle_match = set()
    for v in videos:
        author = (v.get("_author") or "").lower()
        if not author:
            continue
        if handle_lower_brand in author:
            handle_match.add(author)
        if _is_real_ugc_caption(v.get("caption") or "", tag):
            counts[author] = counts.get(author, 0) + 1

    # Strong signals: handle-name match OR caption-filter pass
    strong = sorted(
        set(list(handle_match) + list(counts.keys())),
        key=lambda h: -counts.get(h, 0),
    )
    strong_set = set(strong)
    # Pass-0 user-search candidates that aren't already strong signals
    weak = [u.lower() for u in user_search_seeds if u.lower() not in strong_set]

    # Strong first (proven), weak second (worth probing)
    return strong + weak


async def _fetch_hashtag_isolated(api, variant, cutoff, max_rounds=3, saturation_threshold=5):
    """Parallel-safe wrapper around _fetch_one_hashtag. Each call has
    its own local seen_ids and videos list; results are merged later
    by _merge_into_videos. Same repeat-query semantics as the
    sequential version."""
    local_videos = []
    local_seen = set()
    await _fetch_one_hashtag(api, variant, local_videos, local_seen, cutoff,
                             max_rounds=max_rounds,
                             saturation_threshold=saturation_threshold)
    return local_videos


async def _fetch_creator_isolated(api, handle, cutoff, tag):
    """Parallel-safe wrapper around _fetch_one_creator. Returns a
    per-creator video list to be merged serially after gather."""
    local_videos = []
    local_seen = set()
    await _fetch_one_creator(api, handle, local_videos, local_seen, cutoff, tag)
    return local_videos


async def _fetch_one_hashtag(api, variant, videos, seen_ids, cutoff, max_rounds=3, saturation_threshold=5):
    """Fetch one hashtag's videos and merge into shared lists.

    Repeat-querying with saturation cutoff. TikTok's hashtag feed
    rotates between calls, so multiple rounds surface new videos. But:

      1. Rotation eventually cycles back to already-seen content
         (diminishing returns).
      2. Long repeat-scrape sessions trigger TikTok rate-limiting,
         which DEGRADES the results we get from later passes
         (creator-walks, other hashtag variants) — that compounds
         badly because pass 3 is where most of our coverage comes from.

    Empirical tuning (verified May 2026 with BeFreed as testbed):

      max_rounds | saturation | total corpus | wall time
      -----------|------------|--------------|-----------
      1 (single) | n/a        | 395 videos   | ~95s
      3          | <5 new     | 440 videos   | ~160s   <-- chosen
      5          | <3 new     | 54 videos    | ~180s   <-- rate-limited
      10         | 2 empty    | 384 videos   | ~440s   <-- rate-limited

    The "until saturation" approach LOOKS right (recursive! exhaustive!)
    but in practice TikTok punishes long sessions by returning degraded
    data on the rest of the search. The 3-round cap with cutoff-5 is
    the empirically-validated sweet spot.

    Failures are swallowed per-round — a missing tag or transient bot-
    detection blip shouldn't kill the whole search."""
    for round_num in range(max_rounds):
        new_this_round = 0
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
                new_this_round += 1
        except AttributeError as e:
            if "'Hashtag' object has no attribute 'id'" in str(e):
                return  # tag doesn't exist — no point retrying
            raise
        except Exception:
            return  # transient error — skip this variant entirely

        # Saturation: if this round added almost nothing, stop early.
        # We check after round 1 so the first round always runs in full.
        if round_num > 0 and new_this_round < saturation_threshold:
            return


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
