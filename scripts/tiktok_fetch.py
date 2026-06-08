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

    # Keyword/niche discovery goes through the tikwm relay over plain HTTP — it
    # does NOT need TikTokApi or Chromium. Dispatch it BEFORE the TikTokApi
    # import so keyword search works even on a minimal install.
    if mode == "keyword":
        keyword = (payload.get("keyword") or "").strip()
        if not keyword:
            fail("missing keyword")
        run_keyword(keyword, days)
        return

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


# ─── Keyword / niche discovery via the tikwm relay (free, no key, no browser) ──
#
# Why a relay: TikTokApi v7 exposes only Search.users (handles) — it has NO
# video/keyword search, so the brand-hashtag scraper structurally cannot
# enumerate a niche. tikwm.com mirrors TikTok's keyword feed over plain HTTP and
# returns untagged niche UGC (videos that don't hashtag any brand), which is
# exactly the corpus a script writer browses. Verified live 2026-06.
#
# Tradeoffs (intentional, see issue): tikwm is an UNOFFICIAL third-party relay —
# no SLA, ToS gray area, can rate-limit or disappear. So: short timeout, fail
# SOFT to an empty list (never crash the whole search), and this is one mode
# among three independent ones (user / hashtag stay on TikTokApi), so tikwm
# going down degrades only keyword search.

TIKWM_SEARCH_URL = "https://www.tikwm.com/api/feed/search"
TIKWM_PAGE_SIZE = 30
TIKWM_MAX_PAGES = 10  # cursor pages; ~30/page → up to ~300 candidates per keyword


def _tikwm_item_to_raw(item: dict) -> Optional[dict]:
    """Map a tikwm /api/feed/search item to our RawVideo shape. Field names
    differ from TikTokApi: title→caption, play_count→view_count, etc. Returns
    None on a malformed/old item."""
    create_ts = item.get("create_time") or 0
    video_id = item.get("video_id") or item.get("id") or ""
    if not create_ts or not video_id:
        return None
    author = (item.get("author") or {}).get("unique_id") or ""
    posted_at = datetime.fromtimestamp(int(create_ts), tz=timezone.utc)
    return {
        "platform": "tiktok",
        "external_id": str(video_id),
        "posted_at": posted_at.isoformat(),
        "caption": (item.get("title") or "")[:1000],
        "thumbnail_url": item.get("origin_cover") or item.get("cover") or "",
        "video_url": (
            f"https://www.tiktok.com/@{author}/video/{video_id}"
            if author
            else f"https://www.tiktok.com/video/{video_id}"
        ),
        "view_count": int(item.get("play_count", 0) or 0),
        "like_count": int(item.get("digg_count", 0) or 0),
        "comment_count": int(item.get("comment_count", 0) or 0),
        "share_count": int(item.get("share_count", 0) or 0),
        "_author": author,
    }


def _tikwm_fetch_page(keyword: str, cursor: int) -> Optional[dict]:
    """One tikwm search page. Returns the parsed `data` object or None on any
    failure (network, non-JSON/Cloudflare, code!=0). Fails soft by design."""
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode({"keywords": keyword, "count": TIKWM_PAGE_SIZE, "cursor": cursor})
    url = f"{TIKWM_SEARCH_URL}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
        doc = json.loads(body)
    except Exception:
        return None  # network / timeout / non-JSON (Cloudflare challenge)
    if not isinstance(doc, dict) or doc.get("code") != 0:
        return None
    data = doc.get("data")
    return data if isinstance(data, dict) else None


def run_keyword(keyword: str, days: int) -> None:
    """Niche/keyword discovery: page tikwm's keyword feed, map to RawVideo,
    stop at the days cutoff, page ceiling, or when the relay says no more.
    Synchronous (plain HTTP). Always prints a JSON array — empty on total
    failure, never crashes (tikwm is best-effort)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    seen_ids: set[str] = set()
    cursor = 0

    for _ in range(TIKWM_MAX_PAGES):
        data = _tikwm_fetch_page(keyword, cursor)
        if data is None:
            break  # relay failed — return whatever we have so far (fail soft)
        items = data.get("videos") or data.get("data") or []
        if not items:
            break
        for item in items:
            raw = _tikwm_item_to_raw(item)
            if raw is None or raw["external_id"] in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            seen_ids.add(raw["external_id"])
            videos.append(raw)
        if not data.get("hasMore"):
            break
        next_cursor = data.get("cursor")
        if next_cursor is None or int(next_cursor) <= cursor:
            break  # guard against a stuck/looping cursor
        cursor = int(next_cursor)

    print(json.dumps(videos))


def _user_video_cap() -> int:
    """How many of a creator's recent posts to pull in `user` (competitor-
    catalog) mode. Default 300 — enough to be a real catalog view, not the old
    60-post teaser, while the days-window stop still bounds runtime. Override
    with UGCSPY_USER_VIDEO_CAP. Bigger pulls increase throttle exposure (see the
    rate-limit notes on _concurrency_limit), so keep it sane."""
    raw = os.environ.get("UGCSPY_USER_VIDEO_CAP", "")
    try:
        n = int(raw)
        if n >= 1:
            return n
    except (ValueError, TypeError):
        pass
    return 300


async def run_user(handle: str, days: int) -> None:
    """Pull ONE creator's full catalog (their own posts), browser-free.

    Primary path is the yt-dlp flat-playlist walk — the SAME complete-catalog
    walk the hashtag mode's coverage pass uses. It hits www.tiktok.com directly
    (no Chromium, no key) and reaches the creator's oldest posts, so this is a
    true catalog view, not the old ~50-post TikTokApi teaser (which was capped,
    needed a live browser session, and crashed 'No valid sessions found' from a
    datacenter host). NO brand filter here — user mode returns ALL the creator's
    videos within the day window. The TS layer ranks by views.

    TikTokApi is kept only as a fallback for when yt-dlp returns nothing (e.g.
    a handle it can't resolve), and only if a browser session is available."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    seen: set[str] = set()

    # Primary: yt-dlp full-catalog walk (browser-free, complete).
    catalog = await asyncio.to_thread(_ytdlp_creator_catalog, handle)
    for raw in catalog:
        ext = raw.get("external_id")
        if not ext or ext in seen:
            continue
        try:
            posted_at = datetime.fromisoformat(raw["posted_at"])
        except (ValueError, KeyError, TypeError):
            continue
        if posted_at < cutoff:
            continue
        seen.add(ext)
        raw.pop("_author", None)
        videos.append(raw)

    # Fallback: legacy TikTokApi path, only if yt-dlp yielded nothing AND a
    # browser session is available. Browser-free is the default, so this is
    # skipped unless UGCSPY_USE_CHROMIUM=1 makes a session creatable.
    if not videos and os.environ.get("UGCSPY_USE_CHROMIUM", "").strip() == "1":
        api = None
        try:
            api = await _create_api()
            user = api.user(handle)
            async for video in user.videos(count=_user_video_cap()):
                d = video.as_dict
                raw = _video_to_raw(d, fallback_handle=handle)
                if raw is None:
                    continue
                posted_at = datetime.fromisoformat(raw["posted_at"])
                if posted_at < cutoff:
                    continue
                raw.pop("_author", None)
                if raw["external_id"] in seen:
                    continue
                seen.add(raw["external_id"])
                videos.append(raw)
        except Exception as e:
            # yt-dlp already failed too — surface so the caller sees the empty.
            print(f"[tiktok_fetch] user-mode fallback failed: {e}", file=sys.stderr)
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

    # Browser-free discovery (pure HTTP, can't crash). TWO sources, unioned:
    #   1. keyword search  — creators surfaced by searching brand-related phrases
    #   2. hashtag search  — creators who tagged #<brand> (the browser-free
    #                        replacement for the fragile Chromium hashtag passes)
    # Together these match the Chromium roster WITHOUT the crash/timeout surface.
    kw_scores = _tikwm_discover_scored(
        tag,
        [
            tag, f"{tag} app", f"{tag} reading", f"reading with {tag}",
            f"{tag} microlearning", f"{tag} book", f"{tag} learning", f"{tag} podcast",
            f"{tag} psychology", f"{tag} productivity", f"{tag} self improvement",
            f"{tag} communication", f"{tag} confidence",
        ],
        pages=15,
    )
    tikwm_tag = _tikwm_discover_by_hashtag(tag)
    # Hashtag-sourced creators tagged #<brand> explicitly — a strong signal — so
    # give them a baseline score that floats them up the walk order.
    for h in tikwm_tag:
        kw_scores[h] = kw_scores.get(h, 0) + 3
    # Source 3: following-graph SNOWBALL. Brand-UGC creators mutually-follow as a
    # tight collective, so walking who the high-signal seeds FOLLOW surfaces the
    # long tail that keyword/hashtag search never ranks high enough to return.
    # Seed it with the strongest keyword/hashtag finds (score>=2), depth-1.
    snowball_seeds = [h for h, v in kw_scores.items() if v >= 2] or list(kw_scores.keys())
    snow_scores = _tikwm_snowball_creators(snowball_seeds)
    # A handle followed by N known brand creators gets +2 per follower-seed: being
    # inside the brand's follow-collective is a strong brand signal on its own.
    for h, n in snow_scores.items():
        kw_scores[h] = kw_scores.get(h, 0) + 2 * n
    # Rank ALL candidates by signal: creators surfaced most often (brand title,
    # hashtag, and/or followed by many brand creators) are walked first. Under a
    # budget the high-confidence brand-UGC creators get crawled; the noise tail
    # from broad keywords falls below the cut. Discovery stays WIDE; precision is
    # the yt-dlp coverage pass's job.
    tikwm_seeds = [h for h, _ in sorted(kw_scores.items(), key=lambda kv: -kv[1])]
    strong = sum(1 for v in kw_scores.values() if v >= 2)
    print(
        f"[tiktok_fetch] browser-free discovery: {len(kw_scores)} candidates "
        f"({len(tikwm_tag)} via hashtag, +{len(snow_scores)} via follow-snowball), "
        f"{strong} high-signal (score>=2); walking by signal rank.",
        file=sys.stderr,
    )

    # Chromium discovery is OFF by default now (it crashes/times out and the two
    # tikwm sources cover the same ground). Set UGCSPY_USE_CHROMIUM=1 to re-enable
    # it as an additional source (e.g. on a residential IP where it's stable).
    use_chromium = os.environ.get("UGCSPY_USE_CHROMIUM", "").strip() == "1"

    api = None
    chromium_ok = False
    user_search_seeds: list[str] = []
    try:
        if not use_chromium:
            # Browser-free mode (default): skip Chromium discovery entirely so it
            # can't hang/time out. The two tikwm sources already seeded creators.
            raise RuntimeError("Chromium disabled (browser-free mode is default)")
        api = await _create_api()
        chromium_ok = True

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
    except Exception as e:
        # Chromium discovery (passes 0-2) is fragile. Do NOT let it kill the run:
        # the tikwm seeds + yt-dlp walk (which need no browser) can still produce
        # a strong result. Log to stderr and continue with whatever we have.
        label = "skipped" if not use_chromium else f"failed ({e!s})"
        print(
            f"[tiktok_fetch] Chromium discovery {label}; "
            f"continuing with {len(tikwm_seeds)} tikwm-discovered creators.",
            file=sys.stderr,
        )
    finally:
        if api is not None:
            try:
                await api.__aexit__(None, None, None)
            except Exception:
                pass

    # Pass 3: seed creators — the COVERAGE pass (most genuine UGC comes from
    # walking creators' full catalogs via yt-dlp). Runs OUTSIDE the Chromium
    # try/except: it needs no browser, so a Chromium crash above just means a
    # smaller seed roster, not a dead run. Roster = Chromium-discovered seeds
    # (if any) UNION tikwm-discovered creators (always available).
    #   - breadth: how many creators (UGCSPY_MAX_SEED_CREATORS, default 200).
    #   - concurrency: how many catalog walks at once (UGCSPY_WALK_CONCURRENCY,
    #     default 4). Measured: yt-dlp walks are not meaningfully throttled, so
    #     parallel is safe; the knob is just caution for huge rosters.
    chromium_seeds = _merge_seed_creators(videos, tag, user_search_seeds)
    seed_creators = _union_seeds(chromium_seeds, tikwm_seeds)
    if seed_creators:
        max_seeds = _max_seed_creators()
        walk_conc = _creator_walk_concurrency()
        walk_delay = _creator_walk_delay()
        pass_3_results = await _gather_with_concurrency(
            walk_conc,
            [
                _fetch_creator_isolated(api, c, cutoff, tag, delay=walk_delay)
                for c in seed_creators[:max_seeds]
            ],
        )
        _merge_into_videos(pass_3_results, videos, seen_ids)

    print(json.dumps(videos))


def _union_seeds(chromium_seeds: list[str], tikwm_seeds: list[str]) -> list[str]:
    """Merge the two creator-discovery sources, deduped, preserving order:
    Chromium seeds first (ranked by hit-count in _merge_seed_creators), then any
    tikwm-discovered creators the browser pass missed or couldn't reach."""
    out: list[str] = []
    seen: set[str] = set()
    for h in list(chromium_seeds) + list(tikwm_seeds):
        hl = (h or "").lstrip("@").lower()
        if hl and hl not in seen:
            seen.add(hl)
            out.append(hl)
    return out


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


def _max_seed_creators() -> int:
    """How many discovered creators to walk in pass 3 (the coverage pass).
    Default 200 — was a hard 25, which dropped the long tail of low-volume
    genuine creators. Override with UGCSPY_MAX_SEED_CREATORS. More creators ×
    full yt-dlp catalog walks = more wall time + throttle exposure, so this is
    paired with lowered pass-3 concurrency."""
    raw = os.environ.get("UGCSPY_MAX_SEED_CREATORS", "")
    try:
        n = int(raw)
        if n >= 1:
            return n
    except (ValueError, TypeError):
        pass
    return 200


def _tikwm_discover_creators(
    tag: str, queries: list[str], pages: int = 10, precise: bool = False
) -> list[str]:
    """Discover creators who post about `tag` via the tikwm keyword search —
    PURE HTTP, no Chromium/TikTokApi session. This is the browser-free discovery
    path: the Chromium passes (0-2) are fragile ('No valid sessions found' kills
    discovery and silently shrinks the creator roster), so we union in a source
    that can't fail that way.

    DISCOVERY IS DELIBERATELY WIDE (precise=False, the default). We collect
    EVERY creator the brand search surfaces, NOT just the ones whose currently-
    surfaced video title happens to contain the brand token. Rationale: the
    coverage pass (yt-dlp) walks each creator's FULL catalog and re-applies the
    per-video brand filter (_is_real_ugc_caption) on every post. A creator whose
    search-result video doesn't mention the brand in its TITLE (e.g. a spoken-
    only promo, or a title about the topic not the brand) still has genuine
    brand UGC elsewhere in their catalog — dropping them here loses all of it.
    Filtering at discovery was the bug that shrank a ~200-creator brand search
    down to ~38. Precision is the coverage pass's job, not discovery's.

    Pass precise=True only when you want discovery itself to be conservative
    (e.g. for a noise-prone generic tag where walking every surfaced creator's
    full catalog would be wasteful)."""
    return sorted(_tikwm_discover_scored(tag, queries, pages, precise).keys())


def _tikwm_discover_scored(
    tag: str, queries: list[str], pages: int = 10, precise: bool = False
) -> dict[str, int]:
    """Like _tikwm_discover_creators, but returns {handle: signal_score} instead
    of a flat list. The score = how many times the creator was surfaced by the
    brand search across all queries/pages. A creator surfaced multiple times is
    far more likely to be a genuine brand-UGC creator than a one-off keyword
    co-occurrence, so the coverage pass can walk high-score creators FIRST and,
    under a budget, spend its yt-dlp walks where they pay off. Captions that
    pass the per-video brand filter add a bonus (a direct brand mention in the
    surfaced title is the strongest single signal)."""
    scores: dict[str, int] = {}
    for kw in queries:
        cursor = 0
        for _ in range(pages):
            data = _tikwm_fetch_page(kw, cursor)
            if not data:
                break
            for item in (data.get("videos") or []):
                author = (item.get("author") or {}).get("unique_id")
                if not author:
                    continue
                caption = item.get("title") or ""
                is_brand_title = _is_real_ugc_caption(caption, tag)
                if precise and not is_brand_title:
                    continue
                h = author.lower()
                # +1 per surfacing, +2 bonus when the surfaced title itself
                # carries the brand (strongest signal).
                scores[h] = scores.get(h, 0) + 1 + (2 if is_brand_title else 0)
            if not data.get("hasMore"):
                break
            nxt = data.get("cursor")
            if nxt is None or int(nxt) <= cursor:
                break
            cursor = int(nxt)
    return scores


def _tikwm_challenge_id(tag: str) -> Optional[str]:
    """Resolve a hashtag NAME to tikwm's numeric challenge_id (needed because
    the challenge-posts endpoint takes an id, not a name). PURE HTTP."""
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode({"keywords": tag, "count": 10})
    url = f"https://www.tikwm.com/api/challenge/search?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(doc, dict) or doc.get("code") != 0:
        return None
    data = doc.get("data") or {}
    challenges = data.get("challenge_list") or data.get("challenges") or []
    cleaned = tag.lstrip("#@").lower()
    # Prefer an exact name match; else take the first result.
    for c in challenges:
        name = (c.get("cha_name") or c.get("title") or "").lstrip("#").lower()
        cid = c.get("challenge_id") or c.get("id")
        if cid and name == cleaned:
            return str(cid)
    for c in challenges:
        cid = c.get("challenge_id") or c.get("id")
        if cid:
            return str(cid)
    return None


def _tikwm_discover_by_hashtag(tag: str, pages: int = 20) -> list[str]:
    """Discover creators who tagged #<tag> via tikwm's challenge-posts endpoint —
    PURE HTTP, no browser. This is the browser-free replacement for the Chromium
    hashtag passes: it walks the #befreed challenge feed and collects every
    creator who posted under it. Returns handles whose caption genuinely carries
    the brand. Empty list if the challenge can't be resolved (fails soft)."""
    import urllib.parse
    import urllib.request

    cid = _tikwm_challenge_id(tag)
    if not cid:
        return []
    found: set[str] = set()
    cursor = 0
    for _ in range(pages):
        qs = urllib.parse.urlencode({"challenge_id": cid, "count": 30, "cursor": cursor})
        url = f"https://www.tikwm.com/api/challenge/posts?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            break
        if not isinstance(doc, dict) or doc.get("code") != 0:
            break
        data = doc.get("data") or {}
        items = data.get("videos") or []
        if not items:
            break
        for item in items:
            author = (item.get("author") or {}).get("unique_id")
            # WIDE discovery: every creator under the #<brand> challenge is a
            # brand signal by construction (they tagged it). Don't re-filter on
            # the surfaced video's title — the yt-dlp coverage pass applies the
            # per-video brand filter across each creator's full catalog. Dropping
            # a creator here because one title lacks the token loses their UGC.
            if author:
                found.add(author.lower())
        if not data.get("hasMore"):
            break
        nxt = data.get("cursor")
        if nxt is None or int(nxt) <= cursor:
            break
        cursor = int(nxt)
    return sorted(found)


def _tikwm_user_id(handle: str) -> Optional[str]:
    """Resolve a TikTok handle to tikwm's numeric user id (needed because the
    /following endpoint takes a numeric id, not a handle). PURE HTTP."""
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode({"unique_id": handle.lstrip("@")})
    url = f"https://www.tikwm.com/api/user/info?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(doc, dict) or doc.get("code") != 0:
        return None
    uid = ((doc.get("data") or {}).get("user") or {}).get("id")
    return str(uid) if uid else None


def _snowball_pages() -> int:
    """How many /following pages to pull per seed in the snowball pass. Default 1
    (depth-1, one page) — depth-2 or many pages explodes the candidate set and
    throttle exposure. Override with UGCSPY_SNOWBALL_PAGES; 0 disables snowball."""
    raw = os.environ.get("UGCSPY_SNOWBALL_PAGES", "")
    try:
        n = int(raw)
        if n >= 0:
            return n
    except (ValueError, TypeError):
        pass
    return 1


def _tikwm_snowball_creators(seed_handles: list[str], max_seeds: int = 60) -> dict[str, int]:
    """Following-graph snowball discovery (depth-1). Brand-UGC creators form a
    tight mutually-following collective, so walking who the known seeds FOLLOW
    surfaces long-tail creators that keyword/hashtag search never ranks high
    enough to return (verified: keyword search missed @bobby/@eilisa/@lance/
    @paige.befreed that the follow-graph finds). PURE HTTP, browser-free.

    Returns {handle: score} where score reflects how many seeds follow that
    handle — a creator followed by MANY known brand creators is very likely a
    brand creator too. Discovery stays WIDE: no caption filter here; the yt-dlp
    coverage pass applies per-video brand precision. Bounded by max_seeds (don't
    resolve+walk thousands of seeds) and _snowball_pages() (depth, default 1)."""
    import time
    import urllib.parse
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    pages = _snowball_pages()
    if pages <= 0 or not seed_handles:
        return {}

    def _followings_for(seed: str) -> list[str]:
        """Resolve one seed -> its followed handles. Pure-HTTP, no shared state,
        so it's safe to run many of these in a thread pool. Retries once on a
        transient throttle so a Cloudflare blip doesn't silently zero the seed."""
        uid = _tikwm_user_id(seed)
        if not uid:
            return []
        out: list[str] = []
        cursor = 0
        for _ in range(pages):
            qs = urllib.parse.urlencode({"user_id": uid, "count": 50, "cursor": cursor})
            url = f"https://www.tikwm.com/api/user/following?{qs}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
            doc = None
            for attempt in range(2):  # one retry on throttle/transient error
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        doc = json.loads(resp.read().decode("utf-8", "replace"))
                    if isinstance(doc, dict) and doc.get("code") == 0:
                        break
                except Exception:
                    doc = None
                time.sleep(1.5 * (attempt + 1))
            if not isinstance(doc, dict) or doc.get("code") != 0:
                break
            data = doc.get("data") or {}
            flist = data.get("followings") or data.get("following") or []
            if not flist:
                break
            for u in flist:
                h = (u.get("unique_id") or "").lower()
                if h:
                    out.append(h)
            if not data.get("hasMore"):
                break
            nxt = data.get("cursor")
            if nxt is None or (str(nxt).isdigit() and int(nxt) <= cursor):
                break
            try:
                cursor = int(nxt)
            except (ValueError, TypeError):
                break
        return out

    # Resolve seeds with BOUNDED concurrency. Each seed is ~2 HTTP round-trips
    # (user/info + user/following). Serial across ~150 seeds is the discovery
    # bottleneck (~2min), but a wide pool (12+) trips tikwm's Cloudflare rate
    # limit — which fails SILENTLY: throttled calls return errors, the seed
    # yields nothing, and the snowball quietly contributes ~0 (this is exactly
    # why an over-parallel snowball made the roster no bigger than search alone).
    # 4 workers is the measured safe point: ~3x faster than serial, no throttle.
    # Override with UGCSPY_SNOWBALL_WORKERS.
    try:
        workers = int(os.environ.get("UGCSPY_SNOWBALL_WORKERS", "") or 4)
        workers = max(1, min(workers, 8))
    except (ValueError, TypeError):
        workers = 4
    seeds = seed_handles[:max_seeds]
    scores: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(seeds) or 1)) as pool:
        for handles in pool.map(_followings_for, seeds):
            for h in handles:
                scores[h] = scores.get(h, 0) + 1
    return scores


def _creator_walk_concurrency() -> int:
    """How many creator catalog-walks run at once in pass 3. Default 4.
    Measured: yt-dlp's creator/item_list walk is NOT meaningfully rate-limited —
    8 consecutive walks across two rounds returned identical per-creator counts
    (149/91/129/55), and a creator's count reflects their real catalog size, not
    throttling. So parallel walks are safe; UGCSPY_WALK_CONCURRENCY is left as a
    knob only for caution on very large rosters.

    Default 8: with wide discovery now surfacing a ~200-creator high-signal
    roster (up from ~38), 4-way walking was the wall-time bottleneck. 8-way
    halves it with no measured throttle cost. Lower it if you ever see empties."""
    raw = os.environ.get("UGCSPY_WALK_CONCURRENCY", "")
    try:
        n = int(raw)
        if n >= 1:
            return n
    except (ValueError, TypeError):
        pass
    return 8


def _creator_walk_delay() -> float:
    """Seconds slept BETWEEN creator walks. Default 0 (fast). SLOW CRAWL = set
    UGCSPY_WALK_DELAY=3 to space requests and avoid rate limits."""
    raw = os.environ.get("UGCSPY_WALK_DELAY", "")
    try:
        d = float(raw)
        if d >= 0:
            return d
    except (ValueError, TypeError):
        pass
    return 0.0


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


async def _fetch_creator_isolated(api, handle, cutoff, tag, delay: float = 0.0):
    """Parallel-safe wrapper around _fetch_one_creator. Returns a per-creator
    video list to be merged serially after gather. When `delay` > 0, sleeps that
    many seconds AFTER the walk — in slow-crawl mode (concurrency=1) this spaces
    consecutive creator walks to avoid tripping TikTok rate limits."""
    local_videos = []
    local_seen = set()
    await _fetch_one_creator(api, handle, local_videos, local_seen, cutoff, tag)
    if delay > 0:
        await asyncio.sleep(delay)
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
    """Mirror of TS isHashtagMatch — does this caption carry the brand via
    hashtag, @mention, OR plain-text brand token? The plain-text form (#5)
    recovers high-reach genuine UGC that writes the brand name without a # or @
    (e.g. 'reading with befreed is so clutch') — verified to add real videos
    and zero junk because the token equals the brand name. Used to qualify seed
    creators. Python lookbehind needs fixed width, so we use (?<![...]) classes."""
    import re
    if not caption:
        return False
    escaped = re.escape(tag.lstrip("@#"))
    pattern = re.compile(
        r"#" + escaped + r"(?![a-z0-9_])|"
        r"#" + escaped + r"_\d+|"
        r"#" + escaped + r"app(?![a-z0-9_])|"
        r"@" + escaped + r"(?![a-z0-9_])|"
        r"(?<![a-z0-9_])" + escaped + r"(?![a-z0-9_])",  # plain-text brand token
        re.IGNORECASE,
    )
    return bool(pattern.search(caption))


# ─── yt-dlp creator catalog walk (defeats the TikTokApi per-creator cap) ──────
#
# Why yt-dlp instead of TikTokApi for the per-creator walk:
#   TikTokApi v7's user.videos() is broken in 2026 — the `count` arg is silently
#   clamped to ~30 upstream and cursor pagination doesn't advance (TikTokApi
#   issues #1183/#1105/#1119). So it can only ever return a creator's NEWEST ~50
#   posts, which both caps per-creator coverage AND starves older months (the
#   recency cliff). Measured: top BeFreed creators flatlined at exactly 50-55.
#
#   yt-dlp's native TikTokUserIE walks https://www.tiktok.com/api/creator/
#   item_list/ with a real createTime cursor loop back to the creator's first
#   post — the full public catalog. Verified live: @annaa.learns 55 → 149.
#   `--flat-playlist` returns id/url/title(caption)/timestamp/counts WITHOUT a
#   per-video fetch, so it sidesteps yt-dlp's 2026 per-video extraction breakage
#   AND gives us the caption needed for the brand-precision filter in one shot.
#   It hits www.tiktok.com directly (NOT the Cloudflare-gated tikwm relay), so it
#   works from a datacenter host with no key, no signing, no residential proxy.
#
#   yt-dlp's walk is still occasionally PARTIAL in 2026 (one of three test
#   creators capped at 55) — so the caller falls back to the old TikTokApi path
#   on an empty result, and a future persistent-dedup layer can accumulate
#   partial walks across runs.

def _ytdlp_bin() -> str:
    """Resolve the yt-dlp binary. This script runs under the managed venv's
    python (the TS layer spawns ~/.ugcspy/venv/bin/python), so yt-dlp installed
    in that venv sits next to sys.executable. Prefer it; else fall back to PATH."""
    candidate = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.exists(candidate):
        return candidate
    return "yt-dlp"  # rely on PATH (system install)


def _ytdlp_creator_catalog(handle: str, max_retries: int = 3) -> list[dict]:
    """Return a creator's full public catalog as RawVideo dicts via yt-dlp's
    flat-playlist walk. Empty list on failure (caller falls back). No network
    dependency on tikwm — hits www.tiktok.com directly.

    Slow-crawl depth: TikTok truncates a walk mid-way if the item_list pages
    arrive too fast (this is why a creator that yields 149 solo drops to ~64 in a
    hammered run). UGCSPY_YTDLP_SLEEP_REQUESTS (seconds) is passed to yt-dlp's
    --sleep-requests so it spaces its INTERNAL paging — the lever that actually
    keeps deep walks deep. Default 0 (fast)."""
    import subprocess
    import time

    url = f"https://www.tiktok.com/@{handle.lstrip('@')}"
    cmd = [
        _ytdlp_bin(),
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
    ]
    sleep_req = os.environ.get("UGCSPY_YTDLP_SLEEP_REQUESTS", "").strip()
    try:
        if sleep_req and float(sleep_req) > 0:
            cmd += ["--sleep-requests", sleep_req]
    except ValueError:
        pass
    cmd.append(url)

    # Per-creator walk timeout. A real catalog dumps in well under 60s; the old
    # 600s only ever helped a pathological handle while risking a 10-minute stall
    # that drags out a 200-creator roster. 120s default, override via
    # UGCSPY_YTDLP_TIMEOUT. A timeout drops to TikTokApi fallback (or empty).
    try:
        walk_timeout = int(os.environ.get("UGCSPY_YTDLP_TIMEOUT", "") or 120)
        if walk_timeout < 10:
            walk_timeout = 120
    except (ValueError, TypeError):
        walk_timeout = 120

    last_err = ""
    for attempt in range(max_retries):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=walk_timeout)
            if proc.returncode != 0:
                last_err = (proc.stderr or "")[-200:]
                time.sleep(2 * (attempt + 1))  # backoff before retry
                continue
            doc = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            last_err = str(e)[-200:]
            time.sleep(2 * (attempt + 1))
            continue
        entries = doc.get("entries") or []
        out: list[dict] = []
        for e in entries:
            raw = _ytdlp_entry_to_raw(e, fallback_handle=handle)
            if raw is not None:
                out.append(raw)
        return out
    return []  # all retries failed → caller falls back to TikTokApi


def _ytdlp_entry_to_raw(e: dict, fallback_handle: str) -> Optional[dict]:
    """Map a yt-dlp --flat-playlist entry to our RawVideo shape. yt-dlp field
    names differ from TikTokApi: title/description→caption, timestamp→posted_at,
    repost_count→share_count. Returns None on a malformed entry."""
    video_id = e.get("id") or ""
    ts = e.get("timestamp")
    if not video_id or not ts:
        return None
    author = e.get("uploader") or e.get("channel") or fallback_handle.lstrip("@")
    posted_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return {
        "platform": "tiktok",
        "external_id": str(video_id),
        "posted_at": posted_at.isoformat(),
        "caption": (e.get("title") or e.get("description") or "")[:1000],
        "thumbnail_url": e.get("thumbnail") or "",
        "video_url": e.get("url")
        or f"https://www.tiktok.com/@{author}/video/{video_id}",
        "view_count": int(e.get("view_count", 0) or 0),
        "like_count": int(e.get("like_count", 0) or 0),
        "comment_count": int(e.get("comment_count", 0) or 0),
        "share_count": int(e.get("repost_count", 0) or 0),
        "_author": author,
    }


async def _fetch_one_creator(api, handle, videos, seen_ids, cutoff, tag):
    """Walk a creator's FULL catalog (via yt-dlp) and merge any posts that pass
    the brand-precision filter + date cutoff. Falls back to the (capped, broken)
    TikTokApi path only if yt-dlp returns nothing, so a yt-dlp break degrades
    coverage rather than killing the pass. Per-creator failures are swallowed."""
    # Primary: yt-dlp full-catalog walk (defeats the per-creator cap).
    # _ytdlp_creator_catalog shells out via blocking subprocess.run; run it in a
    # thread so the asyncio event loop can actually interleave the concurrent
    # walks. Calling it directly would block the loop and serialize every walk to
    # 1-at-a-time (the bug that made a 200-creator roster take ~1h instead of
    # ~8min), defeating _gather_with_concurrency entirely.
    catalog = await asyncio.to_thread(_ytdlp_creator_catalog, handle)
    if catalog:
        for raw in catalog:
            ext = raw["external_id"]
            if ext in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            if not _is_real_ugc_caption(raw.get("caption") or "", tag):
                continue
            seen_ids.add(ext)
            # Match the legacy shape: _author is stripped downstream by the TS
            # provider mapping (or kept for hashtag/keyword paths). Leave it.
            videos.append(raw)
        return

    # Fallback: the old TikTokApi path (newest ~50 only, but better than zero
    # when yt-dlp is blocked/broken for this handle). Skipped when there's no
    # live Chromium session (api is None because the discovery passes crashed) —
    # in that case yt-dlp is the only path and an empty walk just yields nothing.
    if api is None:
        return
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
