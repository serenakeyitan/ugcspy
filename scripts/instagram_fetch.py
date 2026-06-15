#!/usr/bin/env python3
"""Browser-free Instagram fetch bridge — the IG sibling of tiktok_fetch.py.

Reads a JSON request on stdin, writes a JSON response on stdout. Same wire
contract as tiktok_fetch.py: the TS layer (instagram-oss.ts) spawns this in the
managed venv (~/.ugcspy/venv) and parses RawVideo[]-shaped JSON.

Data source is a HYBRID of two OSS tools driven by a logged-in IG browser
session (the bakeoff in DESIGN.md established this is the only free path that
gets BOTH a roster walk AND view counts):

  1. gallery-dl  — walk a creator's /reels/ + /posts/ roster: shortcode, likes,
     caption, a downloadable video_url. FAST and bulk. (No view counts — IG's
     listing endpoint omits them.)
  2. instaloader — enrich each shortcode via its single-post GraphQL call, which
     DOES return video_view_count + video_play_count. (instaloader's own
     profile pagination is 403-blocked under raw-cookie auth, so we never use it
     to walk — gallery-dl supplies the shortcodes; instaloader only enriches.)

AUTH: a live logged-in IG session. Cookies are exported from a browser
(default: safari) via yt-dlp's cookie machinery. A missing sessionid surfaces as
a clear "re-login required" error, never a silent empty result.

Modes:
  {"mode": "user", "handle": "...", "days": N}  -> RawVideo[] for that creator
  {"mode": "session_check"}                     -> {"logged_in": bool, ...}

Output (success): {"videos": [RawVideo, ...]}
Output (error):   {"error": "...", "code": "re_login_required|..."}
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import http.cookiejar
from datetime import datetime, timedelta, timezone

# Which browser holds the logged-in IG session. Override with UGCSPY_IG_COOKIE_BROWSER.
COOKIE_BROWSER = os.environ.get("UGCSPY_IG_COOKIE_BROWSER", "safari")
# Politeness sleep between per-post instaloader enrich calls (IG rate-limits hard
# on the authenticated GraphQL endpoint). Override with UGCSPY_IG_ENRICH_SLEEP.
ENRICH_SLEEP_S = float(os.environ.get("UGCSPY_IG_ENRICH_SLEEP", "1.2"))
# Cap how many roster posts to enrich per run (each is a GraphQL call → cost +
# rate-limit exposure). Override with UGCSPY_IG_MAX_ENRICH.
MAX_ENRICH = int(os.environ.get("UGCSPY_IG_MAX_ENRICH", "40"))


def _emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _fail(msg, code="error"):
    _emit({"error": msg, "code": code})
    sys.exit(0)  # exit 0: the error is in-band JSON, parsed by the TS layer


def export_ig_cookies(dest_path):
    """Export the logged-in IG cookies from the browser to a Netscape file.

    Returns (path, has_session). has_session is False when no `sessionid` is
    present — i.e. the browser is logged OUT of Instagram.
    """
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception as e:  # pragma: no cover - import guard
        _fail(f"yt-dlp not available for cookie export: {e}", "deps_missing")

    try:
        jar = extract_cookies_from_browser(COOKIE_BROWSER)
    except Exception as e:
        _fail(
            f"Could not read {COOKIE_BROWSER} cookies for Instagram: {e}. "
            f"Set UGCSPY_IG_COOKIE_BROWSER to a browser logged into Instagram.",
            "cookie_read_failed",
        )

    out = http.cookiejar.MozillaCookieJar(dest_path)
    has_session = False
    n = 0
    for c in jar:
        if "instagram" in (c.domain or ""):
            out.set_cookie(c)
            n += 1
            if c.name == "sessionid" and c.value:
                has_session = True
    out.save(ignore_discard=True, ignore_expires=True)
    return dest_path, has_session, n


def galler_dl_roster(handle, cookies_path, limit):
    """Walk a creator's roster via gallery-dl. Returns a list of dicts with
    shortcode / likes / caption / video_url / username / date — but NO views."""
    handle = handle.lstrip("@")
    posts = {}
    # Walk both the reels tab (video-first) and the grid posts; dedupe by shortcode.
    for tab in ("reels", "posts"):
        url = f"https://www.instagram.com/{handle}/{tab}/"
        cmd = [
            sys.executable, "-m", "gallery_dl",
            "--cookies", cookies_path,
            "--no-download",
            "--range", f"1-{limit}",
            "-j", url,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            continue
        if res.returncode != 0 and not res.stdout.strip():
            # login redirect / rate-limit → stderr carries the reason
            continue
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for row in data:
            if not (isinstance(row, list) and len(row) >= 3 and isinstance(row[2], dict)):
                continue
            md = row[2]
            sc = md.get("shortcode")
            if not sc or sc in posts:
                continue
            # only keep video posts (reels) — that's what we transcribe/track
            posts[sc] = {
                "shortcode": sc,
                "likes": md.get("likes") if isinstance(md.get("likes"), int) else 0,
                "comments": md.get("comments") if isinstance(md.get("comments"), int) else 0,
                "caption": md.get("description") or "",
                "video_url": md.get("video_url") or "",
                "thumbnail_url": md.get("display_url") or "",
                "username": (md.get("username") or md.get("owner") or handle),
                "date": md.get("date") or md.get("post_date"),
                "is_video": bool(md.get("video_url")),
            }
    return list(posts.values())


# Web GraphQL doc_id for IG's PolarisPostActionLoadPostQueryQuery — returns one
# post's full media (incl. view/play counts) in a SINGLE POST. Far lighter than
# instaloader's Post.from_shortcode (multiple requests/post + a rate-controller
# that re-inits each run and double-counts against gallery-dl in the same
# session). Tunable if IG rotates it.
_IG_POST_DOC_ID = os.environ.get("UGCSPY_IG_POST_DOC_ID", "8845758582119845")
_IG_WEB_APP_ID = "936619743392459"
_IG_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _load_cookie_dict(cookies_path):
    cj = http.cookiejar.MozillaCookieJar(cookies_path)
    cj.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in cj if "instagram" in (c.domain or "")}


def _fetch_post_media(shortcode, cookies):
    """ONE GraphQL POST → the post's media node (or None). Raises on HTTP error so
    the caller can detect a throttle (403/429)."""
    import urllib.request
    import urllib.parse

    body = urllib.parse.urlencode({
        "variables": json.dumps({
            "shortcode": shortcode,
            "fetch_tagged_user_count": None,
            "hoisted_comment_id": None,
            "hoisted_reply_id": None,
        }),
        "doc_id": _IG_POST_DOC_ID,
    }).encode()
    req = urllib.request.Request("https://www.instagram.com/graphql/query", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("X-CSRFToken", cookies.get("csrftoken", ""))
    req.add_header("X-IG-App-ID", _IG_WEB_APP_ID)
    req.add_header("User-Agent", _IG_UA)
    req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return ((d.get("data") or {}).get("xdt_shortcode_media")) or None


import re as _re

# IG throttle detection. instaloader raises ConnectionException/QueryReturned*
# with the HTTP status + message in the text. We must NOT loose-substring match
# (codex P2): "Post ABC401 does not exist" contains "401" but is just a dead
# post, and a bare "401 Unauthorized" is an EXPIRED SESSION (re-login), not a
# rate-limit. So:
#   - 429 / "too many requests" / explicit rate-limit PHRASES (please wait /
#     rate limit / try again later / temporarily) → ALWAYS throttle, with or
#     without a status code (these phrases are unambiguously rate-limit).
#   - bare 403 / 401 (no rate phrase) → NOT a throttle: a dead-post "...401..."
#     or a plain expired-session 401 is a different problem. EXCEPTION: a
#     graphql/query 403 is IG's measured enrich-endpoint rate-limit response.
_RATE_PHRASES = ("please wait", "rate limit", "try again later", "temporarily")
_HARD_THROTTLE = _re.compile(r"\b429\b|too many requests", _re.I)


def _is_throttle(err):
    m = str(err).lower()
    # 429 / "too many requests" — unconditional.
    if _HARD_THROTTLE.search(m):
        return True
    # Explicit rate-limit phrases — unconditional (no status digit required).
    if any(p in m for p in _RATE_PHRASES):
        return True
    # graphql/query 403 is IG's standard rate-limit on the enrich endpoint.
    if "graphql/query" in m and _re.search(r"\b403\b", m):
        return True
    return False


def enrich_views(posts, cookies_path, max_enrich=None):
    """Add view_count/play_count to each video post via a single direct GraphQL
    POST per shortcode (_fetch_post_media). Returns (posts, enriched_count,
    throttled).

    Each enriched post gets p["views_enriched"]=True ONLY on a real view fetch;
    posts WITHOUT it must not have their stored view_count overwritten with 0.
    The POST is authoritative on is_video (the hashtag listing isn't), so images
    discovered under a tag get is_video=False here and the caller drops them. On
    an IG throttle (403/429) we BACK OFF: stop immediately, mark throttled, leave
    the rest un-enriched (likes/caption stay fresh). max_enrich caps ATTEMPTS.
    """
    import urllib.error

    cap = max_enrich if isinstance(max_enrich, int) and max_enrich > 0 else MAX_ENRICH
    try:
        cookies = _load_cookie_dict(cookies_path)
    except Exception:
        return posts, 0, False

    attempted = 0
    enriched = 0
    throttled = False
    for p in posts:
        if not p.get("is_video"):
            continue
        if attempted >= cap:
            break
        attempted += 1
        try:
            media = _fetch_post_media(p["shortcode"], cookies)
            if media is None:
                continue
            p["is_video"] = bool(media.get("is_video"))
            if not p["is_video"]:
                continue  # image post discovered under the tag — caller drops it
            metric = media.get("video_play_count") or media.get("video_view_count")
            if metric:
                p["view_count"] = int(metric)
                p["views_enriched"] = True
                enriched += 1
            # backfill fields the listing may have omitted
            if not p.get("video_url"):
                p["video_url"] = f"https://www.instagram.com/reel/{p['shortcode']}/"
            owner = media.get("owner") or {}
            if not p.get("username") and owner.get("username"):
                p["username"] = owner["username"]
        except urllib.error.HTTPError as e:
            # 403/429 = throttle → stop the whole run (continuing deepens it).
            if e.code in (403, 429, 401) or _is_throttle(e):
                throttled = True
                break
            # other HTTP error on one post → skip just it
        except Exception as e:
            if _is_throttle(e):
                throttled = True
                break
            # non-throttle (dead/private post) → skip just it
        time.sleep(ENRICH_SLEEP_S)
    return posts, enriched, throttled


# When a post's date is missing/unparseable, fall back to the UNIX EPOCH, NOT
# now(). A partial gallery-dl row mapped to now() looks "freshly posted" and can
# falsely trip the daemon's 24h relative-breakout filter (codex P2). Epoch can
# never look fresh; the absolute view-threshold path scans all-history and
# ignores the date, so this is the safe default for both alert modes.
_EPOCH_ISO = datetime.fromtimestamp(0, tz=timezone.utc).isoformat()


def _iso(dateval):
    if not dateval:
        return _EPOCH_ISO
    if isinstance(dateval, (int, float)):
        return datetime.fromtimestamp(dateval, tz=timezone.utc).isoformat()
    # gallery-dl emits "YYYY-MM-DD HH:MM:SS"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(dateval), fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    # Unparseable, non-empty → epoch too (don't persist a bogus string as a date).
    return _EPOCH_ISO


def to_raw_video(p):
    """Map an enriched post dict to the RawVideo contract (matches tiktok_fetch).

    views_enriched signals whether view_count came from a REAL enrichment this
    run. False (un-enriched or throttled) → the TS layer must NOT overwrite a
    stored view_count with this row's 0 (reuse last-known instead)."""
    return {
        "platform": "instagram",
        "external_id": p["shortcode"],
        "posted_at": _iso(p.get("date")),
        "caption": p.get("caption", ""),
        "thumbnail_url": p.get("thumbnail_url", ""),
        "video_url": p.get("video_url") or f"https://www.instagram.com/reel/{p['shortcode']}/",
        "view_count": int(p.get("view_count", 0)),
        "like_count": int(p.get("likes", 0)),
        "comment_count": int(p.get("comments", 0)),
        "share_count": 0,  # IG does not expose shares to scraping
        "author_handle": (p.get("username") or "").lstrip("@") or None,
        "views_enriched": bool(p.get("views_enriched", False)),
    }


def resolve_walk_limits(req_limit, req_max_enrich, roster_min):
    """Derive (roster_limit, max_enrich) from the request. Pure + testable.

    Roster walk: gallery-dl walks newest-first with no date filter, so we pull a
    generous window (floor roster_min) to keep RECENT videos refreshing each tick.
    Very old videos beyond this depth keep last-seen counts (same as the TikTok
    free path); a regularly-polling daemon re-walks the top each tick.

    Enrich cap: an EXPLICIT tier (interactive search quick/standard/deep) caps how
    many posts get VIEW enrichment, for the speed/depth tradeoff. With NO tier
    (the daemon poll), enrich the WHOLE walked roster — otherwise videos past the
    cap refresh likes but NOT views, leaving a view-threshold watch firing on
    stale counts (codex P2 round 2)."""
    explicit = req_max_enrich if (isinstance(req_max_enrich, int) and req_max_enrich > 0) else None
    limit = max(int(req_limit or 0), explicit or 0, roster_min)
    max_enrich = explicit if explicit is not None else limit
    return limit, max_enrich


def run_user(req):
    handle = (req.get("handle") or "").strip()
    if not handle:
        _fail("instagram user mode requires a handle", "bad_request")
    roster_min = int(os.environ.get("UGCSPY_IG_ROSTER_LIMIT", "60"))
    limit, max_enrich = resolve_walk_limits(req.get("limit"), req.get("max_enrich"), roster_min)

    with tempfile.TemporaryDirectory() as tmp:
        cookies_path = os.path.join(tmp, "ig_cookies.txt")
        _, has_session, n = export_ig_cookies(cookies_path)
        if not has_session:
            _fail(
                f"No logged-in Instagram session in {COOKIE_BROWSER} "
                f"(found {n} IG cookies but no sessionid). Log into Instagram in "
                f"{COOKIE_BROWSER}, or set UGCSPY_IG_COOKIE_BROWSER to a browser "
                f"that is logged in.",
                "re_login_required",
            )
        roster = galler_dl_roster(handle, cookies_path, limit)
        if not roster:
            _fail(
                f"Instagram returned no posts for @{handle} (private, empty, or "
                f"the session was rejected). If this account is public, the IG "
                f"session may have expired — re-login in {COOKIE_BROWSER}.",
                "empty_or_blocked",
            )
        roster, enriched, throttled = enrich_views(roster, cookies_path, max_enrich)
        videos = [to_raw_video(p) for p in roster if p.get("is_video")]
        _emit(
            {
                "videos": videos,
                "enriched_views": enriched,
                "roster_size": len(roster),
                # True → IG rate-limited this run; the caller should keep
                # last-known view counts and warn the user to ease off.
                "throttled": throttled,
            }
        )


def _extract_post_md(row):
    """Pull the post metadata dict out of a gallery-dl -j row, tolerant of both
    shapes: [type, url, meta] (user/roster) and [type, meta] (hashtag/tag). A row
    is a post only if its dict carries a shortcode."""
    for el in row if isinstance(row, list) else ():
        if isinstance(el, dict) and el.get("shortcode"):
            return el
    return None


def gallerydl_hashtag(tag, cookies_path, limit):
    """DISCOVER creators who posted under #tag via gallery-dl's instagram:tag
    extractor (explore/tags/<tag>/). Returns post dicts keyed by shortcode, each
    carrying its OWN creator (username) — that's how 'rank all UGC creators for a
    brand' works. No views yet (enrich_views adds them)."""
    tag = tag.lstrip("#").strip()
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--cookies", cookies_path,
        "--no-download",
        "--range", f"1-{limit}",
        "-j", url,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return []
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []
    posts = {}
    for row in data:
        md = _extract_post_md(row)
        if not md:
            continue
        # Carousel posts emit ONE row per slide, each with its own `shortcode` but
        # a shared `post_shortcode` (the parent). Dedup on the PARENT so a 10-image
        # carousel counts as one post, not ten (would inflate a creator's volume).
        sc = md.get("post_shortcode") or md["shortcode"]
        if sc in posts:
            continue
        owner = md.get("owner") if isinstance(md.get("owner"), dict) else {}
        username = (
            md.get("username")
            or (owner.get("username") if isinstance(owner, dict) else None)
            or md.get("owner_username")
            or ""
        )
        posts[sc] = {
            "shortcode": sc,
            "likes": md.get("likes") if isinstance(md.get("likes"), int) else 0,
            "comments": md.get("comments") if isinstance(md.get("comments"), int) else 0,
            "caption": md.get("description") or "",
            "video_url": md.get("video_url") or "",
            "thumbnail_url": md.get("display_url") or "",
            "username": username,
            "date": md.get("date") or md.get("post_date"),
            # The tag listing omits the media TYPE, so we can't tell reel-vs-image
            # here. Flag optimistically so the post enters enrich_views, which is
            # AUTHORITATIVE on is_video (and corrects images to False). Posts the
            # enrich cap doesn't reach keep this optimistic flag — acceptable: a
            # hashtag is reel-dominated and the fallback URL still resolves.
            "is_video": True,
        }
    return list(posts.values())


def run_hashtag(req):
    tag = (req.get("tag") or req.get("handle") or "").strip()
    if not tag:
        _fail("instagram hashtag mode requires a tag", "bad_request")
    roster_min = int(os.environ.get("UGCSPY_IG_ROSTER_LIMIT", "60"))
    limit, max_enrich = resolve_walk_limits(req.get("limit"), req.get("max_enrich"), roster_min)

    with tempfile.TemporaryDirectory() as tmp:
        cookies_path = os.path.join(tmp, "ig_cookies.txt")
        _, has_session, n = export_ig_cookies(cookies_path)
        if not has_session:
            _fail(
                f"No logged-in Instagram session in {COOKIE_BROWSER} "
                f"(found {n} IG cookies but no sessionid). Log into Instagram in "
                f"{COOKIE_BROWSER}, or set UGCSPY_IG_COOKIE_BROWSER to a browser "
                f"that is logged in.",
                "re_login_required",
            )
        posts = gallerydl_hashtag(tag, cookies_path, limit)
        if not posts:
            _fail(
                f"Instagram returned no posts for #{tag} (the session may have "
                f"expired, or the tag has no recent public posts). Re-login in "
                f"{COOKIE_BROWSER} and retry.",
                "empty_or_blocked",
            )
        posts, enriched, throttled = enrich_views(posts, cookies_path, max_enrich)
        videos = [to_raw_video(p) for p in posts if p.get("is_video")]
        # Apply the trailing-window cutoff (the hashtag listing is NOT date-sorted
        # or date-filtered by IG, so it mixes in old posts). Keep videos with a
        # real posted_at within `days`; drop epoch-dated (unknown-date) rows from a
        # windowed query rather than guess.
        days = req.get("days")
        if isinstance(days, int) and days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            kept = []
            for v in videos:
                try:
                    ts = datetime.fromisoformat(v["posted_at"])
                except (ValueError, KeyError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff and ts > datetime.fromtimestamp(86400, tz=timezone.utc):
                    kept.append(v)
            videos = kept
        _emit(
            {
                "videos": videos,
                "enriched_views": enriched,
                "roster_size": len(posts),
                "distinct_creators": len({v.get("author_handle") for v in videos if v.get("author_handle")}),
                "throttled": throttled,
            }
        )


def run_session_check(_req):
    with tempfile.TemporaryDirectory() as tmp:
        cookies_path = os.path.join(tmp, "ig_cookies.txt")
        _, has_session, n = export_ig_cookies(cookies_path)
        _emit({"logged_in": has_session, "ig_cookie_count": n, "browser": COOKIE_BROWSER})


def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        _fail(f"invalid request JSON: {e}", "bad_request")

    mode = req.get("mode")
    if mode == "user":
        run_user(req)
    elif mode == "hashtag":
        run_hashtag(req)
    elif mode == "session_check":
        run_session_check(req)
    else:
        _fail(
            f"unsupported instagram mode: {mode!r} (supported: user, hashtag, session_check)",
            "bad_request",
        )


if __name__ == "__main__":
    main()
