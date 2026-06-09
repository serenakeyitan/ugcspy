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


def _tikwm_get(url: str, *, retries: int = 3, timeout: int = 15):
    """Robust single GET against tikwm with retry + backoff. The relay is flaky
    in ways that are NOT permanent rate-limits (measured): it intermittently
    returns a transient `code` != 0 envelope, a slow response, or a dropped
    connection, then succeeds on the very next try. The old code treated ANY of
    these as a hard "throttled" signal and aborted the whole walk — which is why
    identical runs swung between 111 and 29 candidates (feed variance, not real
    throttling). Here we distinguish:
      - returns the parsed dict on a clean `code == 0` response;
      - retries (exp backoff) on connection error / timeout / non-zero envelope;
      - returns None only after all retries are exhausted (genuine outage).
    Pure stdlib HTTP, no crash."""
    import time
    import urllib.request

    last = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(doc, dict) and doc.get("code") == 0:
                return doc
            last = doc  # non-zero envelope → transient, retry
        except Exception as e:
            last = e
        if attempt < retries - 1:
            time.sleep(0.8 * (2 ** attempt))  # 0.8, 1.6, 3.2s backoff
    return None


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

    # Browser-free discovery (pure HTTP, can't crash). PURE-HASHTAG model — two
    # complementary sources, no noisy full-text keyword search:
    #   1. ALL brand hashtags — the main #<brand> tag PLUS every campaign-code
    #      and compound variant (#befreed_0124, #usebefreed, ...), each feed
    #      deep-paged. Every creator here came from a real brand HASHTAG, so the
    #      source is high-purity by construction (vs the old full-text keyword
    #      search, which was ~8% pure and forced the walk to chew through noise).
    #   2. Following-graph SNOWBALL — recovers the LOW-VIEW long tail that never
    #      reaches the top pages of any challenge feed (e.g. creators whose brand
    #      videos sit at a few hundred views). They can't be hashtag-surfaced, but
    #      they mutually-follow the core collective, so walking who the core seeds
    #      follow finds them.
    # hit_count from source 1 = how many distinct brand challenges a creator was
    # in; a higher count is a stronger brand signal and floats them up the walk.
    kw_scores = _tikwm_discover_all_brand_hashtags(tag)
    # Bonus for multi-challenge presence so core creators lead the walk order.
    for h in list(kw_scores.keys()):
        kw_scores[h] = kw_scores[h] + 3  # baseline: surfaced by a real brand tag
    # Source 2: SNOWBALL from the strongest hashtag finds (depth-1).
    snowball_seeds = [h for h, v in kw_scores.items() if v >= 4] or list(kw_scores.keys())
    snow_scores = _tikwm_snowball_creators(snowball_seeds)
    # A handle followed by N known brand creators gets +2 per follower-seed: being
    # inside the brand's follow-collective is a strong brand signal on its own.
    for h, n in snow_scores.items():
        kw_scores[h] = kw_scores.get(h, 0) + 2 * n
    # Rank ALL candidates by signal: creators in the most brand challenges and/or
    # followed by many brand creators are walked first. Discovery stays WIDE;
    # final brand precision is the yt-dlp coverage pass's job.
    tikwm_seeds = [h for h, _ in sorted(kw_scores.items(), key=lambda kv: -kv[1])]
    hashtag_n = len(kw_scores) - len(snow_scores)
    strong = sum(1 for v in kw_scores.values() if v >= 5)
    print(
        f"[tiktok_fetch] pure-hashtag discovery: {len(kw_scores)} candidates "
        f"(~{hashtag_n} via brand hashtags, +{len(snow_scores)} via follow-snowball), "
        f"{strong} high-signal (multi-challenge); walking by signal rank.",
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
    #     default 16). Measured: yt-dlp walks are not meaningfully throttled and
    #     a single walk is ~6-7s, so wall-time is serial fan-out — concurrency
    #     divides it directly. Parallel is safe (hits tiktok.com, not tikwm).
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
        # prefer_metrics=True: the yt-dlp walk is authoritative for CURRENT
        # view/like counts. Let it overwrite the stale snapshots the discovery
        # feed (Pass 1/2) captured, instead of first-writer-wins dropping it.
        _merge_into_videos(pass_3_results, videos, seen_ids, prefer_metrics=True, brand_tag=tag)

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


def _is_brand_hashtag(name: str, brand: str) -> bool:
    """True if a hashtag NAME genuinely belongs to the brand. The challenge/search
    endpoint matches loosely, so its result list mixes real brand tags with
    coincidental ones; we filter at the NAME level (no per-video work).

    Keeps:  #befreed, #befreed_0124 (campaign code), #usebefreed (prefix),
            #befreedaffirmations (brand + suffix word)
    Rejects: #befree, #freed, #beafraid (don't contain the full brand token);
             #befreedom (brand token + an ENGLISH-word continuation, i.e. the
             unrelated word 'freedom').

    Rule: the brand token must appear. It qualifies when it sits at a boundary
    (followed by a digit, underscore, separator, or end — the campaign-code and
    most-compound forms), OR when followed by letters that are NOT one of a small
    deny-list of English continuations that form an unrelated word. We lean
    INCLUSIVE: a false keep only adds a candidate the per-video brand filter
    rejects later in the walk; a false reject permanently loses a real creator.
    Purity is ultimately enforced by the catalog walk, not here."""
    import re

    nm = (name or "").lstrip("#@").lower()
    b = brand.lstrip("#@").lower()
    if not nm or not b or b not in nm:
        return False
    # Continuations that turn the brand token into a DIFFERENT English word.
    # Keep this tiny and brand-specific-agnostic; only the most common traps.
    DENY_SUFFIX_STARTS = ("om",)  # befreed+om = "freedom"-style coincidence
    for m in re.finditer(re.escape(b), nm):
        after = nm[m.end() :]
        if not after:
            return True  # brand at end
        if not after[0].isalpha():
            return True  # digit / underscore / separator → campaign code etc.
        if not any(after.startswith(s) for s in DENY_SUFFIX_STARTS):
            return True  # brand + a non-denylisted word (e.g. 'affirmations')
    return False


def _tikwm_all_brand_challenges(brand: str, search_pages: int = 3) -> list[tuple[str, str]]:
    """Enumerate EVERY brand-related challenge (the main #<brand> tag plus all
    campaign-code variants like #befreed_0124 and compounds like #usebefreed),
    each paired with its CORRECT challenge_id.

    Why this exists: tikwm's challenge/search returns the variant tags, but the
    old _tikwm_challenge_id() took count=10 and fell back to "first result",
    which collapsed a variant name onto the wrong (usually the main) challenge_id
    — so a variant's feed was never actually read. Here we read the search list
    in full and keep each (name -> its own id), filtered by _is_brand_hashtag so
    #befree / #freed noise is dropped at the NAME level (no full-text search, no
    per-video work). PURE HTTP.

    Returns a list of (tag_name, challenge_id), deduped by id, main tag first."""
    import urllib.parse
    import urllib.request

    b = brand.lstrip("#@").lower()
    by_id: dict[str, str] = {}
    cursor = 0
    for _ in range(max(1, search_pages)):
        qs = urllib.parse.urlencode({"keywords": b, "count": 30, "cursor": cursor})
        url = f"https://www.tikwm.com/api/challenge/search?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            break
        if not isinstance(doc, dict) or doc.get("code") != 0:
            break
        data = doc.get("data") or {}
        challenges = data.get("challenge_list") or data.get("challenges") or []
        if not challenges:
            break
        for c in challenges:
            name = (c.get("cha_name") or c.get("challenge_name") or c.get("title") or "")
            cid = c.get("challenge_id") or c.get("id")
            if not cid or not _is_brand_hashtag(name, b):
                continue
            cid = str(cid)
            # keep the first (cleanest) name we saw for each distinct id
            by_id.setdefault(cid, name.lstrip("#@").lower())
        if not data.get("hasMore"):
            break
        nxt = data.get("cursor")
        if nxt is None or int(nxt) <= cursor:
            break
        cursor = int(nxt)

    # Order: exact main tag first, then the rest (so the densest feed leads).
    items = [(nm, cid) for cid, nm in by_id.items()]
    items.sort(key=lambda kv: (kv[0] != b, kv[0]))
    return items


def _tikwm_creators_in_challenge(cid: str, pages: int) -> tuple[set[str], bool]:
    """Deep-page ONE challenge's post feed, collecting every author handle.
    Returns (creators, hard_fail). `hard_fail` is True ONLY when a page genuinely
    could not be fetched after retries (real outage) — NOT for normal feed
    variance, which the old code wrongly read as a rate-limit and aborted on.

    Measured reality (why this is robust now): tikwm honors `count` loosely
    (returns 8–18 videos for a requested 30) and occasionally serves a transient
    empty/error page mid-feed while `hasMore` is still true. The fix:
      - every page goes through _tikwm_get (retry + backoff), so a transient
        blip is retried, not fatal;
      - a single empty page does NOT end the walk — we advance the cursor and
        give the feed TOLERANCE consecutive empties before concluding it's truly
        exhausted (real end-of-feed = hasMore False, or the cursor stops moving);
      - hard_fail is reserved for _tikwm_get returning None (all retries failed).
    This makes coverage deterministic across runs instead of swinging with luck."""
    import urllib.parse

    found: set[str] = set()
    cursor = 0
    empty_streak = 0
    TOLERANCE = 2  # consecutive empty/blip pages tolerated before stopping
    for _ in range(max(1, pages)):
        qs = urllib.parse.urlencode({"challenge_id": cid, "count": 30, "cursor": cursor})
        doc = _tikwm_get(f"https://www.tikwm.com/api/challenge/posts?{qs}")
        if doc is None:
            # genuine fetch failure after retries — report so the caller can
            # decide, but return what we gathered (don't lose it).
            return found, True
        data = doc.get("data") or {}
        items = data.get("videos") or []
        for item in items:
            author = (item.get("author") or {}).get("unique_id")
            if author:
                found.add(author.lower())
        has_more = data.get("hasMore")
        nxt = data.get("cursor")
        if not items:
            empty_streak += 1
            if empty_streak >= TOLERANCE or not has_more:
                break
        else:
            empty_streak = 0
        if not has_more:
            break
        if nxt is None or int(nxt) <= cursor:
            # cursor didn't advance: nudge past it once, else stop
            if items:
                cursor += len(items)
                continue
            break
        cursor = int(nxt)
    return found, False


def _tikwm_discover_all_brand_hashtags(
    brand: str, main_pages: int = 40, variant_pages: int = 3
) -> dict[str, int]:
    """OPTIMIZED pure-hashtag discovery: enumerate ALL brand challenges (main +
    every campaign-code/compound variant) and deep-page each feed, unioning the
    creators. Returns {handle: hit_count} where hit_count = how many distinct
    brand challenges that creator appeared in (a strong signal — someone in many
    brand challenges is a core brand creator; floats them up the walk order).

    Replaces the noisy full-text keyword search (Method A): every creator here
    came from a real brand HASHTAG feed, so the source is high-purity by
    construction. The main tag is paged deepest (densest feed); variants are
    paged shallower (each is small — most return everything in 1-2 pages).

    Outage-resilient: each page is fetched via _tikwm_get (retry + backoff), so
    normal feed variance no longer aborts the sweep. Only a genuine fetch failure
    after retries (hard_fail) counts; two hard-fails in a row means the relay is
    actually down, so we stop with what we have. The main tag is read FIRST
    (densest, highest value), so even a mid-sweep outage still yields the core
    roster."""
    import time

    scores: dict[str, int] = {}
    challenges = _tikwm_all_brand_challenges(brand)
    delay = _hashtag_feed_delay()
    main = brand.lstrip("#@").lower()
    consecutive_fail = 0
    for name, cid in challenges:
        pages = main_pages if name == main else variant_pages
        creators, hard_fail = _tikwm_creators_in_challenge(cid, pages)
        for h in creators:
            scores[h] = scores.get(h, 0) + 1
        if hard_fail:
            consecutive_fail += 1
            # Two genuine fetch failures in a row → relay is down; stop with what
            # we have instead of grinding through every remaining challenge.
            if consecutive_fail >= 2:
                print(
                    f"[tiktok_fetch] hashtag sweep stopped early (tikwm unreachable "
                    f"after retries); {len(scores)} creators gathered so far.",
                    file=sys.stderr,
                )
                break
        else:
            consecutive_fail = 0
        if delay > 0:
            time.sleep(delay)
    return scores


def _hashtag_feed_delay() -> float:
    """Seconds to sleep between challenge-feed reads. tikwm is Cloudflare-gated
    and throttles bursty callers; a small gap keeps a multi-challenge sweep under
    the limit. Default 0.3s. Override with UGCSPY_HASHTAG_FEED_DELAY."""
    raw = os.environ.get("UGCSPY_HASHTAG_FEED_DELAY", "")
    try:
        v = float(raw)
        if v >= 0:
            return v
    except (ValueError, TypeError):
        pass
    return 0.3


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
    """How many creator catalog-walks run at once in pass 3 — the dominant cost
    of a search (each walk is a yt-dlp item_list fetch). Measured: yt-dlp's walk
    is NOT meaningfully rate-limited — repeated parallel walks return identical
    per-creator counts (a count reflects real catalog size, not throttling) — and
    a single creator's walk is fast (~6-7s even for a 150-video catalog). So the
    wall-time is dominated by SERIAL fan-out across the roster, which concurrency
    directly divides. Override with UGCSPY_WALK_CONCURRENCY.

    Default 16: with the pure-hashtag roster (~200 creators) the walk fan-out is
    the bottleneck; 16-way roughly halves it vs 8 with no measured throttle cost
    (yt-dlp hits www.tiktok.com directly, not the rate-limited tikwm relay). The
    per-walk caption-truncation rescue does call tikwm, but only for the handful
    of clipped brand videos per creator, so concurrent walks don't burst tikwm.
    Lower it if you ever see empty walks (a sign of local CPU/network limits)."""
    raw = os.environ.get("UGCSPY_WALK_CONCURRENCY", "")
    try:
        n = int(raw)
        if n >= 1:
            return n
    except (ValueError, TypeError):
        pass
    return 16


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


def _merge_into_videos(per_task_results, videos, seen_ids, prefer_metrics=False, brand_tag=""):
    """Serial merge of per-task results into the shared videos list,
    deduplicating by external_id. Runs after all parallel fetches
    complete, so no race conditions.

    prefer_metrics: when True, a result for an already-seen external_id does
    NOT get dropped — instead it UPGRADES the metrics (view/like/comment/share
    counts) and caption of the entry already in `videos`. This matters because
    the discovery passes (tikwm keyword/hashtag feed) snapshot a video's view
    count at the moment it was indexed, which can be weeks stale. The yt-dlp
    creator walk (Pass 3) reports each video's CURRENT counts. First-writer-wins
    dedup would otherwise freeze a video at its stale discovery-feed view count
    even after it went viral — e.g. a clip indexed at 162K that has since grown
    to 2.6M would keep showing 162K, mis-ranking it. So the authoritative walk
    is allowed to overwrite the snapshot's metrics in place."""
    # Index existing entries by external_id only when we may need to update them.
    index = (
        {v.get("external_id"): v for v in videos if v.get("external_id")}
        if prefer_metrics
        else {}
    )
    for task_videos in per_task_results:
        if not task_videos:
            continue
        for v in task_videos:
            ext = v.get("external_id")
            if not ext:
                continue
            if ext in seen_ids:
                if prefer_metrics:
                    _upgrade_metrics(index.get(ext), v, brand_tag)
                continue
            seen_ids.add(ext)
            videos.append(v)
            if prefer_metrics:
                index[ext] = v


def _upgrade_metrics(existing, fresh, brand_tag=""):
    """Enrich an existing video dict from the fresh (authoritative yt-dlp walk)
    copy. Metrics are bumped to the higher value; caption prefers the longer
    (less-truncated) one; and identity fields the discovery feed left BLANK are
    backfilled from the walk. A metric is only overwritten when the fresh value
    is a higher non-zero count, so a walk that momentarily reports 0 (transient
    API hiccup) never regresses a good snapshot.

    Why backfill identity: the tikwm discovery feed sometimes yields a video with
    no author (its item had no author.unique_id), so the row lands with
    author_handle = NULL and renders as "(unknown)" — even though the SAME video
    in the creator's yt-dlp walk carries `_author`. First-writer-wins dedup kept
    the blank discovery copy and the walk's author never got promoted. We now
    fill any identity field that is missing on `existing` but present on `fresh`
    (author + a real per-creator video_url), without ever clobbering a value the
    discovery copy already had."""
    if existing is None or not isinstance(fresh, dict):
        return
    for key in ("view_count", "like_count", "comment_count", "share_count"):
        new = fresh.get(key)
        if isinstance(new, int) and new > (existing.get(key) or 0):
            existing[key] = new
    # Prefer the walk's caption when it's longer (less truncated) — UNLESS doing
    # so would drop a brand tag the existing caption carries. The flat-playlist
    # walk truncates non-deterministically, so its (sometimes longer) caption can
    # still be the one missing the brand hashtag while the discovery-feed caption
    # has it. Replacing a brand-tagged caption with a longer brand-LESS one would
    # silently un-qualify the video. So only swap when the candidate is longer
    # AND not a brand-signal regression.
    fresh_cap = fresh.get("caption") or ""
    cur_cap = existing.get("caption") or ""
    if len(fresh_cap) >= len(cur_cap):
        cur_has_brand = bool(brand_tag) and _is_real_ugc_caption(cur_cap, brand_tag)
        fresh_has_brand = bool(brand_tag) and _is_real_ugc_caption(fresh_cap, brand_tag)
        if not (cur_has_brand and not fresh_has_brand):
            existing["caption"] = fresh_cap
    # Backfill the author from the walk when discovery left it blank. `_author`
    # is the bridge's internal field (TS maps it onto author_handle); also mirror
    # to `author_handle` in case the existing dict already uses that key.
    fresh_author = fresh.get("_author") or fresh.get("author_handle")
    if fresh_author:
        if not (existing.get("_author") or "").strip():
            existing["_author"] = fresh_author
        if not (existing.get("author_handle") or "").strip():
            existing["author_handle"] = fresh_author
    # Backfill a real per-creator video_url if discovery only had a bare one.
    fresh_url = fresh.get("video_url") or ""
    cur_url = existing.get("video_url") or ""
    if fresh_url and ("/@" in fresh_url) and ("/@" not in cur_url):
        existing["video_url"] = fresh_url


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


def _caption_maybe_truncates_brand(caption, tag) -> bool:
    """True when a caption looks like yt-dlp's flat-playlist clipped the brand
    tag mid-word, so the real (untruncated) caption might still carry it.

    THE BUG this guards: yt-dlp's --flat-playlist NON-DETERMINISTICALLY truncates
    a video's caption to ~72 chars. When the brand hashtag sits right at that
    boundary, `#befreed_0124` arrives as `#befree` (or `#befreedX` cut to a
    partial), and _is_real_ugc_caption rejects it — silently dropping a genuine
    (often high-view) brand video. We can't tell from yt-dlp alone, so we detect
    the *signature* of that clip and let the caller re-fetch the full caption
    from tikwm before discarding.

    Signals (any):
      1. A run that is a NON-EMPTY PREFIX of the brand token appears immediately
         after '#' or '@' at/near the very end of the caption (the classic
         '...#befree' cut). Requires len>=3 so we don't rescue on a stray '#b'.
      2. The caption length is at the known truncation ceiling (<=73) AND it
         ends without sentence/space terminator AND the brand prefix's first few
         letters appear — a weaker fallback for odd clips.
    Conservative by design: a false positive only costs one extra tikwm call;
    a false negative loses the video, so we lean slightly toward rescuing."""
    import re
    if not caption:
        return False
    brand = tag.lstrip("@#").lower()
    if len(brand) < 4:
        return False
    cap = caption.rstrip()
    low = cap.lower()
    # yt-dlp marks a clipped caption with a trailing literal "..." ellipsis
    # (the '...#befree...' we saw on the 2.6M purple video). That ellipsis is
    # BOTH the truncation signal and noise that hides the real tail token, so
    # strip a trailing run of dots / unicode ellipsis before inspecting the end.
    had_ellipsis = bool(re.search(r"(\.{2,}|…)\s*$", low))
    low = re.sub(r"(\.{2,}|…)\s*$", "", low).rstrip()
    # Signal 1: trailing "#<prefix>" or "@<prefix>" where prefix is a strict,
    # non-empty (>=3 char) prefix of the brand and the tag is NOT already a
    # complete accepted form (those are handled by _is_real_ugc_caption).
    m = re.search(r"[#@]([a-z0-9_]+)$", low)
    if m:
        frag = m.group(1)
        # full or campaign-suffixed forms are NOT truncation — they'd have passed
        if frag == brand or frag.startswith(brand):
            return False
        if len(frag) >= 3 and brand.startswith(frag) and frag != brand:
            return True
    # Signal 2: caption was explicitly ellipsis-clipped AND the brand's leading
    # bytes appear near the tail — catches clips where the '#' itself got cut or
    # the fragment is shorter than 3 chars but the ellipsis confirms truncation.
    if had_ellipsis and brand[:4] in low[-15:]:
        return True
    # Signal 3 (no ellipsis): caption sits at the known ~72-char ceiling and the
    # brand prefix appears near the tail without a sentence terminator.
    if len(cap) <= 73 and brand[:4] in low[-12:] and not low.endswith((".", "!", "?")):
        return True
    return False


def _tikwm_video_caption(video_id: str, author: str = "x") -> Optional[str]:
    """Fetch ONE video's FULL (untruncated) caption from tikwm. Used to rescue
    walk videos whose flat-playlist caption looks like it clipped the brand tag
    (see _caption_maybe_truncates_brand). tikwm returns the complete description
    in data.title. PURE HTTP, short timeout, None on any failure (caller then
    falls back to the truncated caption / drops the video as before)."""
    import urllib.request

    import time

    url = (
        "https://www.tikwm.com/api/?url="
        f"https://www.tiktok.com/@{author}/video/{video_id}"
    )
    # tikwm is Cloudflare-gated and throttles bursty callers (the snowball can be
    # hammering it concurrently). A single failed call here would silently drop a
    # genuine brand video, so retry a couple times with backoff before giving up.
    for attempt in range(3):
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (ugcspy)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(1.2 * (attempt + 1))
            continue
        if not isinstance(doc, dict) or doc.get("code") != 0:
            # code!=0 is tikwm's throttle/err envelope — retry rather than treat
            # it as an authoritative "no brand tag" (which would drop the video).
            time.sleep(1.2 * (attempt + 1))
            continue
        title = (doc.get("data") or {}).get("title")
        return title if isinstance(title, str) and title else ""
    return None  # all retries failed → caller keeps the truncated caption


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

def _ytdlp_rescue_budget() -> int:
    """Max tikwm full-caption rescue calls per creator walk (see
    _fetch_one_creator). Bounds the cost of recovering brand videos whose
    flat-playlist caption clipped the tag. Default 25 — comfortably covers a
    creator's handful of truncated high-view posts without hammering tikwm.
    Set UGCSPY_YTDLP_RESCUE=0 to disable rescue entirely (pure yt-dlp, faster
    but loses tag-at-boundary videos)."""
    raw = os.environ.get("UGCSPY_YTDLP_RESCUE", "")
    try:
        n = int(raw)
        if n >= 0:
            return n
    except (ValueError, TypeError):
        pass
    return 25


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
        # Bound how many truncation-rescue tikwm calls this walk may make, so a
        # creator whose captions are heavily clipped can't explode wall time or
        # trip tikwm's rate limit. Each rescue is one HTTP call (~0.3-1s).
        rescue_budget = _ytdlp_rescue_budget()
        for raw in catalog:
            ext = raw["external_id"]
            if ext in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            cap = raw.get("caption") or ""
            if not _is_real_ugc_caption(cap, tag):
                # The brand filter rejected this caption. yt-dlp's flat-playlist
                # truncates captions to ~72 chars NON-DETERMINISTICALLY, which
                # can clip the brand tag (`#befreed_0124` -> `#befree`) and drop
                # a genuine — often high-view — brand video. If the caption shows
                # that clip signature, re-fetch the FULL caption from tikwm and
                # re-test before discarding. Otherwise it's a real non-match.
                if rescue_budget > 0 and _caption_maybe_truncates_brand(cap, tag):
                    rescue_budget -= 1
                    full = await asyncio.to_thread(
                        _tikwm_video_caption, ext, raw.get("_author") or handle.lstrip("@")
                    )
                    if full is None:
                        # tikwm was unreachable/throttled — we COULDN'T verify.
                        # The truncation signature (brand prefix clipped right at
                        # the '...') is already strong evidence the tag is there,
                        # and dropping silently is exactly the bug we're fixing
                        # (a high-view brand video vanishing because a rescue call
                        # got rate-limited). So KEEP it with the truncated caption
                        # rather than lose it. A later refresh re-fetches metrics.
                        pass
                    elif not _is_real_ugc_caption(full, tag):
                        # tikwm answered and the FULL caption genuinely lacks the
                        # brand — this was a real non-match (the prefix was
                        # coincidental, e.g. '#befree' of an unrelated word). Drop.
                        continue
                    else:
                        raw["caption"] = full  # verified: keep untruncated caption
                else:
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
