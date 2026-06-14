#!/usr/bin/env python3
"""Bridge between ugcspy CLI and davidteather/TikTok-Api.

Stdin: JSON
  Handle mode:  { "mode": "user",    "handle":  "@glossier",   "days": 30 }
  Hashtag mode: { "mode": "hashtag", "tag":     "liquiddeath", "days": 30 }
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


def _as_int(value) -> Optional[int]:
    """Parse an UNTRUSTED external value (tikwm/yt-dlp cursor, count, ts) as an
    int; None when unparseable. The relay is an unofficial third party — a
    malformed field must fail soft (end-of-feed / dropped item), never crash
    the run with a ValueError traceback."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value, default: int = 0) -> int:
    """_as_int with a default — for metric counts where 0 is the sane fallback."""
    n = _as_int(value)
    return n if n is not None else default


def _safe_ts(value) -> Optional[datetime]:
    """Parse an untrusted epoch timestamp into an aware UTC datetime, or None on
    a non-numeric / out-of-range value. One malformed item drops that ITEM,
    never the whole search."""
    n = _as_int(value)
    if n is None:
        return None
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        fail(f"invalid stdin json: {e}")

    mode = payload.get("mode") or ("user" if "handle" in payload else None)
    # A non-integer `days` must not raise an uncaught ValueError before dispatch
    # — that would bypass the documented {"error": ...} envelope. Fall back to
    # the default rather than failing hard: `days` is an optional window hint.
    try:
        days = int(payload.get("days", 30))
    except (ValueError, TypeError):
        days = 30
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

    # Trending is pure HTTP via the relay's rotating feed — like keyword mode
    # it needs no TikTokApi/Chromium, so dispatch before that import.
    if mode == "trending":
        run_trending(str(payload.get("region") or "US"), days)
        return

    # Snowball mode exposes the follow-graph discovery (the same
    # _tikwm_snowball_creators that powers hashtag discovery's source 2) as a
    # standalone call seeded by USER-supplied creators rather than brand-hashtag
    # finds — "find more creators like these". PURE HTTP via the relay (no
    # TikTokApi/Chromium), so dispatch before that import.
    if mode == "snowball":
        seeds = payload.get("seeds")
        if not isinstance(seeds, list) or not any(str(s).strip() for s in seeds):
            fail("missing seeds")
        run_snowball([str(s) for s in seeds])
        return

    # Transcript mode needs whisper + yt-dlp (+ the bundled static ffmpeg),
    # NOT TikTokApi/Chromium — dispatch it before the TikTokApi import so it
    # works on a core install that added --with-audio but never touched the
    # browser fallbacks. Two forms: {"url": "..."} → one doc object (legacy);
    # {"urls": [...]} → array of docs, ONE model load for the whole batch.
    if mode == "transcript":
        urls = payload.get("urls")
        if isinstance(urls, list) and urls:
            # Keep EVERY input position — the batch contract is one result per
            # url, aligned by index. A blank element becomes a per-item error
            # envelope via _transcribe_one; dropping it here would shift the
            # array and make the caller discard the whole wave as misaligned.
            cleaned = [str(u).strip() for u in urls]
            if not any(cleaned):
                fail("missing urls")
            run_transcript(cleaned, batch=True)
            return
        url = (payload.get("url") or "").strip()
        if not url:
            fail("missing url")
        run_transcript([url], batch=False)
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
    try:
        await api.create_sessions(**kwargs)
    except BaseException:
        # create_sessions failing ('No valid sessions found' is the common case
        # on datacenter hosts) used to leak the already-entered context: a
        # headful Chromium + playwright driver lingering for the rest of the
        # run. Close the browser before propagating.
        try:
            await api.__aexit__(None, None, None)
        except Exception:
            pass
        raise
    return api


def _video_to_raw(d: dict, fallback_handle: Optional[str] = None) -> Optional[dict]:
    """Convert TikTokApi video.as_dict to our RawVideo shape. Returns None if
    the post is too old or malformed."""
    create_ts = d.get("createTime") or 0
    if not create_ts:
        return None
    posted_at = _safe_ts(create_ts)
    if posted_at is None:
        return None
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
        "view_count": _safe_int(stats.get("playCount", 0) or 0),
        "like_count": _safe_int(stats.get("diggCount", 0) or 0),
        "comment_count": _safe_int(stats.get("commentCount", 0) or 0),
        "share_count": _safe_int(stats.get("shareCount", 0) or 0),
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
TIKWM_MAX_BODY = 10 * 1024 * 1024  # 10MB cap on relay responses (untrusted source)


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
                body = resp.read(TIKWM_MAX_BODY + 1)
            if len(body) > TIKWM_MAX_BODY:
                # Untrusted relay: never slurp an unbounded body. Treat an
                # oversized response as a failed attempt (retry, then None).
                raise ValueError("tikwm response exceeded the 10MB cap")
            doc = json.loads(body.decode("utf-8", "replace"))
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
    if not isinstance(item, dict):
        return None  # untrusted relay: a non-object item drops, never crashes
    create_ts = item.get("create_time") or 0
    video_id = item.get("video_id") or item.get("id") or ""
    if not create_ts or not video_id:
        return None
    # author arrives as an object on feed/search but as a PLAIN STRING on some
    # feed/list rotations — one untrusted shape must not crash the run.
    a = item.get("author")
    if isinstance(a, dict):
        uid = a.get("unique_id")
        # unique_id itself is untrusted — a dict/number here is truthy and
        # would get f-string-serialized into the video URL (seen live).
        author = uid if isinstance(uid, str) else ""
    else:
        author = a if isinstance(a, str) else ""
    posted_at = _safe_ts(create_ts)
    if posted_at is None:
        return None  # malformed/hostile timestamp → drop the item, not the run
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
        "view_count": _safe_int(item.get("play_count", 0) or 0),
        "like_count": _safe_int(item.get("digg_count", 0) or 0),
        "comment_count": _safe_int(item.get("comment_count", 0) or 0),
        "share_count": _safe_int(item.get("share_count", 0) or 0),
        "_author": author,
    }


def _tikwm_fetch_page(keyword: str, cursor: int) -> Optional[dict]:
    """One tikwm search page. Returns the parsed `data` object or None only
    after _tikwm_get's retry + backoff is exhausted (genuine outage). The old
    single-shot urlopen made one transient blip silently truncate keyword
    results — the exact 111-vs-29 swing _tikwm_get was written to fix. Fails
    soft by design."""
    import urllib.parse

    qs = urllib.parse.urlencode({"keywords": keyword, "count": TIKWM_PAGE_SIZE, "cursor": cursor})
    url = f"{TIKWM_SEARCH_URL}?{qs}"
    doc = _tikwm_get(url, timeout=20)
    if doc is None:
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
        next_cursor = _as_int(data.get("cursor"))
        if next_cursor is None or next_cursor <= cursor:
            break  # guard against a stuck/looping/non-numeric cursor
        cursor = next_cursor

    print(json.dumps(videos))


def _trending_rounds(default: int = 8) -> int:
    """How many times to hit the trending feed per run. The relay's
    /api/feed/list is a ROTATING feed with no cursor — each call returns a
    small fresh handful (~3-6 items), so coverage comes from repeated calls +
    dedupe, not paging. Clamped: more rounds = more relay load for
    diminishing new ids. Override with UGCSPY_TRENDING_ROUNDS."""
    n = _as_int(os.environ.get("UGCSPY_TRENDING_ROUNDS"))
    if n is None or n <= 0:
        return default
    return min(n, 30)


def run_trending(region: str, days: int) -> None:
    """Network-wide trending feed (蹭热度 lane): what's viral on TikTok right
    now in a region, regardless of brand. Same fail-soft contract as keyword
    mode — always prints a JSON array, empty only on total relay failure.
    Items flow through the standard RawVideo mapper so the whole downstream
    chain (cache, transcript, rebrand) works unchanged."""
    import time
    import urllib.parse

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    region_clean = (region or "US").strip().upper()[:5] or "US"
    videos: list[dict] = []
    seen_ids: set[str] = set()
    rounds = _trending_rounds()
    empty_streak = 0

    for i in range(rounds):
        qs = urllib.parse.urlencode({"region": region_clean, "count": 30})
        doc = _tikwm_get(f"https://www.tikwm.com/api/feed/list?{qs}")
        if doc is None:
            break  # relay down after retries — return what we have (fail soft)
        data = doc.get("data")
        # The relay is untrusted: a "successful" envelope can carry a scalar
        # data value ({"code":0,"data":"temporarily unavailable"}) — that's an
        # empty round, not a crash.
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("videos") or []
        else:
            items = []
        fresh = 0
        for item in items:
            # Trending carries paid placements (is_ad) — a promoted spot is
            # not organic heat, and "what's genuinely viral" is the lane.
            if isinstance(item, dict) and item.get("is_ad"):
                continue
            raw = _tikwm_item_to_raw(item)
            if raw is None or raw["external_id"] in seen_ids:
                continue
            posted_at = datetime.fromisoformat(raw["posted_at"])
            if posted_at < cutoff:
                continue
            seen_ids.add(raw["external_id"])
            videos.append(raw)
            fresh += 1
        # The feed rotates; two consecutive rounds with nothing new means
        # we've drained the current rotation — more calls won't add coverage.
        empty_streak = empty_streak + 1 if fresh == 0 else 0
        if empty_streak >= 2:
            break
        if i + 1 < rounds:
            time.sleep(0.4)

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
    the brand's own posts. BROWSER-FREE by default.

    Two stages (TikTok's single-hashtag feed dedupes hard per-creator, so one
    challenge call is wildly incomplete; we separate "find creators" from "pull
    their videos"):

      STAGE 1 — DISCOVERY (pure HTTP via the tikwm relay, no Chromium).
        Find creator HANDLES from two complementary sources, then union +
        signal-rank into a ranked roster:
          • ALL brand hashtags — the main #<brand> challenge PLUS every
            campaign-code/compound variant (#yourbrand_0124, #useyourbrand), each
            feed deep-paged (_tikwm_discover_all_brand_hashtags).
          • Follow-graph snowball — walk who the high-signal seeds FOLLOW
            (tikwm /api/user/following, depth-1), recovering the low-view long
            tail that never reaches a challenge feed's top pages.

      STAGE 2 — COVERAGE (yt-dlp, 16-way concurrent — UGCSPY_WALK_CONCURRENCY).
        Walk each ranked creator's FULL public catalog from www.tiktok.com
        (not rate-limited; ~6-7s/creator), apply the per-video brand filter,
        and rescue captions yt-dlp clipped at the ~72-char boundary by
        re-fetching the full caption from tikwm before dropping the video.

    Discovery is browser-free; Chromium is an OPTIONAL extra source only
    (UGCSPY_USE_CHROMIUM=1, off by default — it crashes/hangs on most hosts).

    Each task returns its own list; merges happen serially after asyncio.gather
    to avoid races on shared state."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    videos: list[dict] = []
    seen_ids: set[str] = set()

    # Browser-free discovery (pure HTTP, can't crash). PURE-HASHTAG model — two
    # complementary sources, no noisy full-text keyword search:
    #   1. ALL brand hashtags — the main #<brand> tag PLUS every campaign-code
    #      and compound variant (#yourbrand_0124, #useyourbrand, ...), each feed
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
    kw_scores, relay_down = _tikwm_discover_all_brand_hashtags(tag)

    # Chromium discovery is OFF by default now (it crashes/times out and the two
    # tikwm sources cover the same ground). Set UGCSPY_USE_CHROMIUM=1 to re-enable
    # it as an additional source (e.g. on a residential IP where it's stable).
    use_chromium = os.environ.get("UGCSPY_USE_CHROMIUM", "").strip() == "1"

    # An OUTAGE must not masquerade as an authoritative empty: zero creators
    # BECAUSE tikwm was unreachable is not "this brand has no UGC". Fail loud
    # (JSON error + nonzero exit) so the caller can retry, instead of emitting
    # [] with exit 0. A genuinely-empty brand (relay healthy, zero creators)
    # still completes normally below.
    if not kw_scores and relay_down and not use_chromium:
        print(
            "[tiktok_fetch] discovery found 0 creators because tikwm was "
            "unreachable after retries — reporting an outage, not an empty brand.",
            file=sys.stderr,
        )
        fail("tikwm relay unreachable during hashtag discovery (outage, not an empty brand); retry later")

    # Bonus for multi-challenge presence so core creators lead the walk order.
    for h in list(kw_scores.keys()):
        kw_scores[h] = kw_scores[h] + 3  # baseline: surfaced by a real brand tag
    # Source 2: SNOWBALL from the strongest hashtag finds (depth-1), strongest
    # FIRST — _tikwm_snowball_creators caps at 60 seeds, so the order decides
    # WHICH creators get walked. (The old `v >= 4` filter was a no-op after the
    # +3 baseline and left the cap slicing arbitrary dict order.)
    snowball_seeds = [h for h, _ in sorted(kw_scores.items(), key=lambda kv: -kv[1])]
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
    """Concurrency cap for the LEGACY CHROMIUM hashtag-fetch passes only.

    This governs the optional Chromium/TikTokApi discovery passes (active only
    when UGCSPY_USE_CHROMIUM=1) — see _fetch_hashtag_isolated. It does NOT govern
    the default browser-free path: Stage-2 yt-dlp walk concurrency is
    UGCSPY_WALK_CONCURRENCY (default 16, see _creator_walk_concurrency), and the
    pure-HTTP hashtag sweep is paced by UGCSPY_HASHTAG_FEED_DELAY.

    Default 12 — empirically validated against fresh-IP Chromium probes (16
    hashtags, May 2026): c=4 -> 6.8s, c=8 -> 11.4s, c=12 -> 7.0s, c=16 -> 7.0s,
    all 0 errors. Past 12 the per-request latency dominates, so 12 is the
    conservative pick. Override via `UGCSPY_CONCURRENCY=16 ugcspy search ...`
    (Chromium mode only).

    Cautionary tale (commit 2610607): pushing the Chromium scrape aggressively
    AFTER an IP is already throttled returns ZERO videos for ~10-20 minutes —
    not just "less, but slower". Don't bump it without fresh-IP probe data."""
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
            return min(n, 2000)  # clamp: a roster beyond this is hours of walks
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
            nxt = _as_int(data.get("cursor"))
            if nxt is None or nxt <= cursor:
                break
            cursor = nxt
    return scores


def _is_brand_hashtag(name: str, brand: str) -> bool:
    """True if a hashtag NAME genuinely belongs to the brand. The challenge/search
    endpoint matches loosely, so its result list mixes real brand tags with
    coincidental ones; we filter at the NAME level (no per-video work).

    Keeps (for brand 'yourbrand'):
            #yourbrand, #yourbrand_0124 (campaign code), #useyourbrand (prefix),
            #yourbrandvibes (brand + suffix word)
    Rejects: #yourbran, #brand (don't contain the full brand token); tags where
             the brand token continues into an unrelated ENGLISH word (e.g. a
             brand ending in 'freed' picking up 'om' and becoming the ordinary
             word 'freedom').

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
    DENY_SUFFIX_STARTS = ("om",)  # brand ending in 'freed' + om = "freedom"-style coincidence
    for m in re.finditer(re.escape(b), nm):
        after = nm[m.end() :]
        if not after:
            return True  # brand at end
        if not after[0].isalpha():
            return True  # digit / underscore / separator → campaign code etc.
        if not any(after.startswith(s) for s in DENY_SUFFIX_STARTS):
            return True  # brand + a non-denylisted word (e.g. 'affirmations')
    return False


def _tikwm_all_brand_challenges(brand: str, search_pages: int = 3) -> tuple[list[tuple[str, str]], bool]:
    """Enumerate EVERY brand-related challenge (the main #<brand> tag plus all
    campaign-code variants like #yourbrand_0124 and compounds like #useyourbrand),
    each paired with its CORRECT challenge_id.

    Why this exists: tikwm's challenge/search returns the variant tags, but a
    naive name->id resolver (take count=10, fall back to "first result")
    collapses a variant name onto the wrong (usually the main) challenge_id — so
    a variant's feed would never actually be read. Here we read the search list
    in full and keep each (name -> its own id), filtered by _is_brand_hashtag so
    near-miss tags that don't carry the full brand token are dropped at the NAME
    level (no full-text search, no per-video work). PURE HTTP, via _tikwm_get (retry + backoff) — this is the
    FIRST link in the discovery chain, and a single transient blip here used to
    silently zero the entire hashtag search (empty roster, clean exit 0).

    Returns (items, fetch_failed): items is a list of (tag_name, challenge_id),
    deduped by id, main tag first; fetch_failed is True when a page genuinely
    could not be fetched after retries (relay outage), so the caller can tell
    an outage apart from a brand with no hashtags."""
    import urllib.parse

    b = brand.lstrip("#@").lower()
    by_id: dict[str, str] = {}
    cursor = 0
    fetch_failed = False
    for _ in range(max(1, search_pages)):
        qs = urllib.parse.urlencode({"keywords": b, "count": 30, "cursor": cursor})
        url = f"https://www.tikwm.com/api/challenge/search?{qs}"
        doc = _tikwm_get(url, timeout=20)
        if doc is None:
            fetch_failed = True
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
        nxt = _as_int(data.get("cursor"))
        if nxt is None or nxt <= cursor:
            break
        cursor = nxt

    # Order: exact main tag first, then the rest (so the densest feed leads).
    items = [(nm, cid) for cid, nm in by_id.items()]
    items.sort(key=lambda kv: (kv[0] != b, kv[0]))
    if not items:
        reason = (
            "tikwm unreachable after retries"
            if fetch_failed
            else "no brand-matching hashtags in the search results"
        )
        print(
            f"[tiktok_fetch] challenge enumeration for '{b}' came back empty ({reason}).",
            file=sys.stderr,
        )
    return items, fetch_failed


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
        nxt = _as_int(data.get("cursor"))
        if not items:
            empty_streak += 1
            if empty_streak >= TOLERANCE or not has_more:
                break
        else:
            empty_streak = 0
        if not has_more:
            break
        if nxt is None or nxt <= cursor:
            # cursor didn't advance: nudge past it and keep walking.
            if items:
                cursor += len(items)
            else:
                # Transient empty page that ECHOED the cursor (tikwm's usual
                # blip shape): self-advance so the TOLERANCE allowance above can
                # actually fire — breaking here ended the walk on the FIRST
                # mid-feed blip, which is the run-to-run nondeterminism this
                # state machine exists to fix. (empty_streak < TOLERANCE is
                # guaranteed here; the streak check above breaks otherwise.)
                cursor += TIKWM_PAGE_SIZE
            continue
        cursor = nxt
    return found, False


def _tikwm_discover_all_brand_hashtags(
    brand: str, main_pages: int = 40, variant_pages: int = 3
) -> tuple[dict[str, int], bool]:
    """OPTIMIZED pure-hashtag discovery: enumerate ALL brand challenges (main +
    every campaign-code/compound variant) and deep-page each feed, unioning the
    creators. Returns {handle: hit_count} where hit_count = how many distinct
    brand challenges that creator appeared in (a strong signal — someone in many
    brand challenges is a core brand creator; floats them up the walk order).

    History: this replaced the old full-text keyword search ("Method A"), which
    was removed — it was ~8% precise and forced the walk to chew through ~90%
    noise. Every creator here instead came from a real brand HASHTAG feed, so the
    source is high-purity by construction. The main tag is paged deepest (densest
    feed); variants are paged shallower (each is small — usually 1-2 pages).

    Outage-resilient: each page is fetched via _tikwm_get (retry + backoff), so
    normal feed variance no longer aborts the sweep. Only a genuine fetch failure
    after retries (hard_fail) counts; two hard-fails in a row means the relay is
    actually down, so we stop with what we have. The main tag is read FIRST
    (densest, highest value), so even a mid-sweep outage still yields the core
    roster.

    Returns (scores, relay_down): relay_down is True when the relay genuinely
    failed after retries at any point (challenge enumeration or a feed read),
    so the caller can distinguish 'no creators exist' from 'tikwm was down'."""
    import time

    scores: dict[str, int] = {}
    challenges, relay_down = _tikwm_all_brand_challenges(brand)
    delay = _hashtag_feed_delay()
    main = brand.lstrip("#@").lower()
    consecutive_fail = 0
    for name, cid in challenges:
        pages = main_pages if name == main else variant_pages
        creators, hard_fail = _tikwm_creators_in_challenge(cid, pages)
        for h in creators:
            scores[h] = scores.get(h, 0) + 1
        if hard_fail:
            relay_down = True
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
    return scores, relay_down


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


def _tikwm_user_id(handle: str) -> Optional[str]:
    """Resolve a TikTok handle to tikwm's numeric user id (needed because the
    /following endpoint takes a numeric id, not a handle). PURE HTTP, via the
    retry client — with 4 snowball workers hammering tikwm, a single Cloudflare
    blip on this resolution silently zeroed the seed's entire following list."""
    import urllib.parse

    qs = urllib.parse.urlencode({"unique_id": handle.lstrip("@")})
    url = f"https://www.tikwm.com/api/user/info?{qs}"
    doc = _tikwm_get(url, retries=2, timeout=20)
    if doc is None:
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


def _tikwm_snowball_creators(
    seed_handles: list[str],
    max_seeds: int = 60,
    seed_diag: Optional[dict] = None,
) -> dict[str, int]:
    """Following-graph snowball discovery (depth-1). Brand-UGC creators form a
    tight mutually-following collective, so walking who the known seeds FOLLOW
    surfaces long-tail creators that keyword/hashtag search never ranks high
    enough to return (verified: keyword search missed several long-tail
    creators that the follow-graph finds). PURE HTTP, browser-free.

    Returns {handle: score} where score reflects how many seeds follow that
    handle — a creator followed by MANY known brand creators is very likely a
    brand creator too. Discovery stays WIDE: no caption filter here; the yt-dlp
    coverage pass applies per-video brand precision. Bounded by max_seeds (don't
    resolve+walk thousands of seeds) and _snowball_pages() (depth, default 1).

    If `seed_diag` is passed, it's populated with {seed: status} where status is
    the follow-count on success, or a negative sentinel on failure: -1 = the
    following list was blocked/unreadable (tikwm code != 0 — common, ~60% of
    creators), -2 = the handle didn't resolve to a user id. This lets the caller
    report an honest hit-rate ("3 of 8 seeds had readable following lists")
    instead of conflating a blocked seed with one that simply follows no one.
    The hashtag-discovery caller passes nothing and is unaffected."""
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
        transient throttle so a Cloudflare blip doesn't silently zero the seed.

        Records the seed's outcome in seed_diag (when provided) so the caller can
        report an honest hit-rate: a follow-count on success, -2 if the handle
        never resolved, -1 if EVERY page came back blocked/unreadable. A readable
        list that simply has no followings stays 0 — distinct from blocked."""
        uid = _tikwm_user_id(seed)
        if not uid:
            if seed_diag is not None:
                seed_diag[seed] = -2  # handle didn't resolve to a user id
            return []
        out: list[str] = []
        cursor = 0
        any_readable = False  # did at least one page return code == 0?
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
            any_readable = True
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
        if seed_diag is not None:
            # -1 = blocked/unreadable (no page ever returned code 0); else the
            # follow-count (0 is a readable-but-empty list, NOT a failure).
            seed_diag[seed] = len(out) if any_readable else -1
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


def run_snowball(seed_handles: list[str]) -> None:
    """Standalone follow-graph discovery: given USER-supplied seed creators,
    return the creators most of them follow — "find more creators like these".

    This is the same depth-1 follow-graph walk hashtag discovery uses for its
    source 2, but seeded by the user's chosen creators instead of brand-hashtag
    finds. A candidate followed by MANY of the seeds is stylistically close to
    the cluster (creators in a niche mutually follow each other), so the score
    (how many seeds follow it) IS the similarity signal.

    Emits a JSON ENVELOPE:
      {"creators": [{"handle","seedsFollowing"}, ...sorted by score desc],
       "seedResults": [{"handle","status"}, ...]}
    where status is the seed's follow-count, or -1 (blocked/unreadable list) or
    -2 (handle didn't resolve). The envelope lets the caller report an honest
    hit-rate — most following lists are private/blocked (~60%), so an empty
    `creators` is the NORMAL case and must be distinguishable from a relay
    failure. The seeds themselves are excluded from `creators` (a creator can't
    be its own recommendation). Fail-soft throughout — never a crash."""
    # Normalize: strip @, lowercase, dedupe while preserving order. The score
    # is seed-COUNT, so a duplicated seed must not double-count its followings.
    seen: set[str] = set()
    seeds: list[str] = []
    for raw in seed_handles:
        h = str(raw).strip().lstrip("@").lower()
        if h and h not in seen:
            seen.add(h)
            seeds.append(h)
    if not seeds:
        fail("no valid seed handles")

    seed_diag: dict[str, int] = {}
    scores = _tikwm_snowball_creators(seeds, seed_diag=seed_diag)
    # Drop the seeds themselves — they're inputs, not recommendations.
    creators = [
        {"handle": h, "seedsFollowing": n}
        for h, n in scores.items()
        if h not in seen
    ]
    creators.sort(key=lambda r: (-r["seedsFollowing"], r["handle"]))
    # Seeds capped by max_seeds may never have been walked — only report those
    # that were (present in seed_diag); the rest are simply unknown.
    seed_results = [
        {"handle": h, "status": seed_diag.get(h)}
        for h in seeds
        if h in seed_diag
    ]
    print(json.dumps({"creators": creators, "seedResults": seed_results}))


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
            return min(n, 64)  # clamp: past this it's just local CPU/net thrash
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
    — many will be noise (unrelated accounts whose names merely contain
    the brand string) that get filtered out in pass 3 when their captions
    don't match.

    We pull from two queries: `tag` and `tagapp`, since the official
    account often uses the latter (e.g. @yourbrandapp)."""
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
         these are dedicated UGC accounts like @creator.<brand>)
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

    Empirical tuning (verified May 2026 against the mid-size brand we
    benchmarked):

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
    `#yourbrand_0117` mentioned in the captions of pass-1 results, that's
    a live campaign code worth querying directly for more coverage.

    Returns a sorted list of unique codes (max 12 to bound runtime)."""
    import re
    code_pattern = re.compile(r"#" + re.escape(tag) + r"_(\d{2,4})\b", re.IGNORECASE)
    seen_codes = set()
    for v in videos:
        for match in code_pattern.finditer(v.get("caption") or ""):
            # Keep the code VERBATIM: hashtag names are literal strings, so
            # zero-padding '#yourbrand_117' to 'yourbrand_0117' queried a different
            # (almost certainly empty) hashtag and never read the real feed.
            seen_codes.add(match.group(1))
    # Cap at 12 to bound wall time (each adds ~3-8s scrape)
    return sorted(seen_codes)[:12]


def _is_real_ugc_caption(caption, tag):
    """Mirror of TS isHashtagMatch — does this caption carry the brand via
    hashtag, @mention, OR plain-text brand token? The plain-text form (#5)
    recovers high-reach genuine UGC that writes the brand name without a # or @
    (e.g. 'obsessed with <brand> right now') — verified to add real videos
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
    boundary, `#yourbrand_0124` arrives as `#yourbr` (or `#yourbrandX` cut to a
    partial), and _is_real_ugc_caption rejects it — silently dropping a genuine
    (often high-view) brand video. We can't tell from yt-dlp alone, so we detect
    the *signature* of that clip and let the caller re-fetch the full caption
    from tikwm before discarding.

    Signals (any):
      1. A run that is a NON-EMPTY PREFIX of the brand token appears immediately
         after '#' or '@' at/near the very end of the caption (the classic
         '...#yourbr' cut). Requires len>=3 so we don't rescue on a stray '#b'.
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
    # (the '...#yourbr...' shape we saw on a rescued 2.6M-view brand video).
    # That ellipsis is BOTH the truncation signal and noise that hides the real
    # tail token, so strip a trailing run of dots / unicode ellipsis before
    # inspecting the end.
    had_ellipsis = bool(re.search(r"(\.{2,}|…)\s*$", low))
    low = re.sub(r"(\.{2,}|…)\s*$", "", low).rstrip()
    # Signal 1: trailing "#<prefix>" or "@<prefix>" where prefix is a strict,
    # non-empty (>=3 char) prefix of the brand and the tag is NOT already a
    # complete accepted form (those are handled by _is_real_ugc_caption).
    m = re.search(r"[#@]([a-z0-9_]+)$", low)
    if m:
        frag = m.group(1)
        if frag.startswith(brand):
            # frag EXTENDS the brand token. Complete accepted forms (#yourbrand,
            # #yourbrand_0124, #yourbrandapp) pass _is_real_ugc_caption and never
            # reach this function — so a brand-extending frag here is either a
            # clipped accepted form ('#yourbrand_0124' cut at the underscore
            # arrives as '#yourbrand_'; '#yourbrandapp' cut to '#yourbranda')
            # which we rescue, or a genuinely different tag (the brand token
            # continuing into an unrelated word) which we drop.
            rest = frag[len(brand):]
            return rest == "_" or bool(rest and "app".startswith(rest))
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
#   recency cliff). Measured: the benchmark brand's top creators flatlined at
#   exactly 50-55.
#
#   yt-dlp's native TikTokUserIE walks https://www.tiktok.com/api/creator/
#   item_list/ with a real createTime cursor loop back to the creator's first
#   post — the full public catalog. Verified live: one test creator's walk went
#   from 55 to 149 videos.
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
            return min(n, 200)  # clamp: rescue calls hit the rate-limited relay
    except (ValueError, TypeError):
        pass
    return 25


def _ytdlp_bin() -> str:
    """Resolve the yt-dlp binary. This script runs under the managed venv's
    python (the TS layer spawns the venv interpreter WITHOUT activating it, so
    the venv's bin/Scripts dir is NOT on PATH), and yt-dlp installed in that
    venv sits next to sys.executable — `yt-dlp` on POSIX, `yt-dlp.exe` in a
    Windows venv's Scripts dir. Prefer those; else fall back to PATH."""
    base = os.path.dirname(sys.executable)
    for name in ("yt-dlp", "yt-dlp.exe"):
        candidate = os.path.join(base, name)
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
            # encoding/errors pinned: yt-dlp emits UTF-8 JSON, but text=True
            # alone decodes with the LOCALE encoding and errors='strict' — on a
            # non-UTF-8 locale an emoji-laden caption raised UnicodeDecodeError
            # (a ValueError, NOT json.JSONDecodeError) and escaped the except
            # below, aborting the whole multi-minute search.
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=walk_timeout,
            )
            if proc.returncode != 0:
                last_err = (proc.stderr or "")[-200:]
                time.sleep(2 * (attempt + 1))  # backoff before retry
                continue
            doc = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            last_err = str(e)[-200:]
            time.sleep(2 * (attempt + 1))
            continue
        if not isinstance(doc, dict):
            # valid JSON but not the expected object — treat as a failed attempt
            last_err = f"yt-dlp emitted non-object JSON ({type(doc).__name__})"
            time.sleep(2 * (attempt + 1))
            continue
        entries = doc.get("entries") or []
        out: list[dict] = []
        for e in entries:
            raw = _ytdlp_entry_to_raw(e, fallback_handle=handle)
            if raw is not None:
                out.append(raw)
        return out
    # All retries failed → caller falls back to TikTokApi (or empty). Say WHY:
    # a silent [] makes a broken/blocked yt-dlp extractor indistinguishable from
    # "this creator posted nothing in the window".
    print(
        f"[tiktok_fetch] yt-dlp walk failed for @{handle.lstrip('@')} after "
        f"{max_retries} attempts: {last_err}",
        file=sys.stderr,
    )
    return []


def _ytdlp_entry_to_raw(e: dict, fallback_handle: str) -> Optional[dict]:
    """Map a yt-dlp --flat-playlist entry to our RawVideo shape. yt-dlp field
    names differ from TikTokApi: title/description→caption, timestamp→posted_at,
    repost_count→share_count. Returns None on a malformed entry."""
    if not isinstance(e, dict):
        return None  # a non-object entry drops, never crashes the walk
    video_id = e.get("id") or ""
    ts = e.get("timestamp")
    if not video_id or not ts:
        return None
    author = e.get("uploader") or e.get("channel") or fallback_handle.lstrip("@")
    posted_at = _safe_ts(ts)
    if posted_at is None:
        return None  # malformed timestamp → drop the entry, not the walk
    return {
        "platform": "tiktok",
        "external_id": str(video_id),
        "posted_at": posted_at.isoformat(),
        "caption": (e.get("title") or e.get("description") or "")[:1000],
        "thumbnail_url": e.get("thumbnail") or "",
        "video_url": e.get("url")
        or f"https://www.tiktok.com/@{author}/video/{video_id}",
        "view_count": _safe_int(e.get("view_count", 0) or 0),
        "like_count": _safe_int(e.get("like_count", 0) or 0),
        "comment_count": _safe_int(e.get("comment_count", 0) or 0),
        "share_count": _safe_int(e.get("repost_count", 0) or 0),
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
    try:
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
                    # can clip the brand tag (`#yourbrand_0124` -> `#yourbr`) and drop
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
                            # coincidental: the clipped tag belonged to an
                            # unrelated word). Drop.
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
    except Exception as e:
        # Docstring contract: per-creator failures are swallowed. One bad walk
        # must degrade to a skipped creator (keeping whatever it already merged),
        # never abort the whole multi-minute pass-3 gather with a raw traceback.
        print(f"[tiktok_fetch] creator walk failed for @{handle}: {e}", file=sys.stderr)
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


# ---------------------------------------------------------------------------
# Transcript mode — spoken narrative + talking/non-talking signal for ONE video.
#
# Pipeline: yt-dlp downloads the audio track only (~1-2MB, no video), Whisper
# transcribes it, and the result is normalized so music beds can't masquerade
# as speech. The normalization logic is a parity copy of
# vendor/video-recipe/scripts/transcribe.py::_result_to_doc — duplicated here
# (not imported) because the npm-packed CLI ships scripts/ but NOT vendor/,
# and the bridge must stay self-contained. test_transcript_mode.py asserts the
# two stay behaviorally identical; change them together.

# A Whisper segment with no_speech_prob above this is treated as non-speech
# (music bed / ambience / silence) and its text is BLANKED — Whisper
# hallucinates plausible lyrics over music, and fake lyrics must not make a
# montage look like a talking video. 0.6 matches Whisper's own decoder default.
TRANSCRIPT_NO_SPEECH_PROB = 0.6

# Maximum audio bytes we'll hand to Whisper. A TikTok video is minutes long;
# anything bigger than this is a mis-resolved URL (livestream, playlist), not
# a UGC clip.
TRANSCRIPT_MAX_AUDIO_BYTES = 100 * 1024 * 1024


def _transcript_non_lexical_re():
    import re

    # Non-lexical vocalizations (sighs, "mmm", "uh", bracketed cues like
    # [Music]) — real audio events, but not scripted speech.
    return re.compile(
        r"^[\s\W]*(?:"
        r"u+h+|u+m+|m+h+m+|h+m+|m+m+|a+h+|o+h+|e+r+|hmm+|uh-huh|mm-hmm|"
        r"\[.*?\]|\(.*?\)|♪+"
        r")[\s\W]*$",
        re.IGNORECASE,
    )


def _lexical_word_count(text: str) -> int:
    """Word count that works for languages WITHOUT spaces. `text.split()` calls
    a whole Chinese/Japanese sentence ONE word, so a fully-narrated Mandarin
    video (e.g. Pingo AI's Chinese-tutor creators) would land under the
    talking threshold. CJK characters count ~1 word each (Chinese averages
    ~1.5 chars/word — close enough for an 8-word gate); everything else counts
    by whitespace tokens."""
    import re

    cjk = re.findall(r"[぀-ヿ㐀-䶿一-鿿가-힯]", text)
    rest = re.sub(r"[぀-ヿ㐀-䶿一-鿿가-힯]", " ", text)
    return len(cjk) + len(rest.split())


def _transcript_normalize(result: dict) -> dict:
    """Whisper raw result → transcript doc with per-segment kind tags and a
    whole-track audio_kind summary ("speech" | "music" | "mixed").

    Parity contract with vendor/video-recipe/scripts/transcribe.py
    (_result_to_doc): same thresholds, same kind tags, same audio_kind rules.
    Extra key on top of that contract: lexical_word_count (talking-classifier
    input, derived from speech-segment text so it works without per-word
    timestamps)."""
    non_lexical = _transcript_non_lexical_re()
    segments_in = result.get("segments") or []
    segments: list[dict] = []
    words: list[dict] = []
    duration = 0.0
    speech_seg_count = 0
    nonspeech_seg_count = 0
    lexical_word_count = 0
    for seg in segments_in:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
            no_speech_prob = float(seg.get("no_speech_prob", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        text = str(seg.get("text", "")).strip()
        if end > duration:
            duration = end

        if no_speech_prob >= TRANSCRIPT_NO_SPEECH_PROB:
            nonspeech_seg_count += 1
            segments.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": "",
                    "kind": "non_speech",
                    "no_speech_prob": round(no_speech_prob, 3),
                }
            )
            continue

        kind = "non_lexical" if (text and non_lexical.match(text)) else "speech"
        if kind == "speech":
            speech_seg_count += 1
            lexical_word_count += _lexical_word_count(text)
        segments.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "kind": kind,
                "no_speech_prob": round(no_speech_prob, 3),
            }
        )
        if kind != "speech":
            continue
        for w in seg.get("words", []) or []:
            wstart = float(w.get("start", w.get("startTime", start)))
            wend = float(w.get("end", w.get("endTime", end)))
            wtext = str(w.get("word", w.get("text", ""))).strip()
            if not wtext:
                continue
            words.append({"start": round(wstart, 3), "end": round(wend, 3), "word": wtext})

    if speech_seg_count == 0 and nonspeech_seg_count > 0:
        audio_kind = "music"
    elif nonspeech_seg_count > 0:
        audio_kind = "mixed"
    else:
        audio_kind = "speech"

    return {
        "language": result.get("language"),
        "duration_sec": round(duration, 3),
        "segments": segments,
        "words": words,
        "audio_kind": audio_kind,
        "lexical_word_count": lexical_word_count,
    }


def _transcript_download_audio(url: str, tmpdir: str) -> str:
    """Download ONLY the audio track to tmpdir; returns the file path.
    bestaudio keeps the transfer at ~1-2MB for a TikTok clip and Whisper reads
    the container directly via ffmpeg — no conversion pass needed."""
    import glob
    import subprocess

    out_tmpl = os.path.join(tmpdir, "audio.%(ext)s")
    # "--" stops yt-dlp option parsing so a crafted URL can't inject flags.
    cmd = [
        _ytdlp_bin(),
        "-f",
        "bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "-o",
        out_tmpl,
        "--",
        url,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-400:]
        raise RuntimeError(f"yt-dlp audio download failed: {tail or 'no stderr'}")
    files = glob.glob(os.path.join(tmpdir, "audio.*"))
    if not files:
        raise RuntimeError("yt-dlp reported success but produced no audio file")
    path = files[0]
    if os.path.getsize(path) > TRANSCRIPT_MAX_AUDIO_BYTES:
        raise RuntimeError(
            f"audio track exceeds {TRANSCRIPT_MAX_AUDIO_BYTES} bytes — not a short-form clip"
        )
    return path


def _resolve_ffmpeg() -> Optional[str]:
    """ffmpeg resolution for a machine with NOTHING installed: prefer a system
    ffmpeg, else the static binary the imageio-ffmpeg pip package ships inside
    the venv (installed by `install-deps --with-audio`). None only when both
    are absent (e.g. --with-audio was installed before imageio-ffmpeg joined
    requirements-audio.txt)."""
    import shutil

    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _load_audio_pcm(path: str, ffmpeg_exe: str):
    """Decode an audio file to the float32 mono 16kHz array Whisper expects.
    Same pipeline as whisper.load_audio, but with an EXPLICIT ffmpeg path —
    whisper's own loader shells out to a bare `ffmpeg` on PATH, which doesn't
    exist on a fresh machine (the imageio static binary isn't named ffmpeg, so
    PATH tricks can't help)."""
    import subprocess

    import numpy as np

    cmd = [
        ffmpeg_exe,
        "-nostdin",
        "-threads",
        "0",
        "-i",
        path,
        "-f",
        "s16le",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError(f"ffmpeg decode failed: {tail or 'no stderr'}")
    return np.frombuffer(proc.stdout, np.int16).flatten().astype(np.float32) / 32768.0


def _transcribe_one(url: str, model, model_name: str, ffmpeg_exe: str) -> dict:
    """Download + transcribe ONE url; returns a doc dict, or an {"error": ...}
    envelope (never raises) so one bad video can't sink a batch."""
    import shutil
    import tempfile

    if not url.startswith(("http://", "https://")):
        return {"error": "transcript url must be http(s)", "video_url": url}
    tmpdir = tempfile.mkdtemp(prefix="ugcspy-transcript-")
    try:
        audio_path = _transcript_download_audio(url, tmpdir)
        audio = _load_audio_pcm(audio_path, ffmpeg_exe)
        # fp16 warnings on CPU are harmless; whisper falls back to fp32 itself.
        result = model.transcribe(audio)
        doc = _transcript_normalize(result if isinstance(result, dict) else {})
        doc["video_url"] = url
        doc["whisper_model"] = model_name
        return doc
    except RuntimeError as e:
        return {"error": str(e), "video_url": url}
    except Exception as e:  # noqa: BLE001 — one video's failure must surface as data, not a traceback
        return {"error": f"transcription failed: {type(e).__name__}: {e}", "video_url": url}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_transcript(urls: list[str], batch: bool) -> None:
    """Transcribe one or many video URLs with ONE model load (the load costs
    ~3-5s + a one-time ~140MB download — per-video reloads dominated wall time
    on multi-video runs). Single form (batch=False) emits one doc object and
    exits nonzero on failure (the original contract); batch form emits a JSON
    array aligned with the input order, where each element is a doc or an
    {"error": ...} envelope, and exits 0 if at least one video succeeded."""
    try:
        import whisper
    except ImportError:
        fail(
            "whisper not installed in the active interpreter. Run `ugcspy install-deps --with-audio` (one-time, ~3-5min + ~1.5GB)."
        )
    ffmpeg_exe = _resolve_ffmpeg()
    if ffmpeg_exe is None:
        fail(
            "no ffmpeg available — re-run `ugcspy install-deps --with-audio` (bundles a static ffmpeg via imageio-ffmpeg; no system install needed)."
        )

    model_name = os.environ.get("UGCSPY_WHISPER_MODEL", "base").strip() or "base"
    try:
        model = whisper.load_model(model_name)
    except Exception as e:  # noqa: BLE001
        fail(f"whisper model '{model_name}' failed to load: {type(e).__name__}: {e}")

    docs = []
    for i, url in enumerate(urls):
        docs.append(_transcribe_one(url, model, model_name, ffmpeg_exe))
        print(f"transcript {i + 1}/{len(urls)} done", file=sys.stderr, flush=True)

    if not batch:
        doc = docs[0]
        if "error" in doc:
            fail(doc["error"])
        print(json.dumps(doc))
        return
    print(json.dumps(docs))
    if all("error" in d for d in docs):
        sys.exit(1)


if __name__ == "__main__":
    main()
