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
from datetime import datetime, timezone

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


def _make_instaloader(cookies_path):
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_comments=False, save_metadata=False, quiet=True,
    )
    cj = http.cookiejar.MozillaCookieJar(cookies_path)
    cj.load(ignore_discard=True, ignore_expires=True)
    for c in cj:
        L.context._session.cookies.set(c.name, c.value, domain="instagram.com")
    return instaloader, L


def enrich_views(posts, cookies_path, max_enrich=None):
    """Add view_count/play_count to each video post via instaloader single-post
    GraphQL (the call that returns counts; profile pagination is blocked, but we
    don't need it — gallery-dl gave us the shortcodes).

    max_enrich caps how many posts to enrich (each is a ~4s GraphQL call). The
    caller (TS layer) sets this from the user's depth tier; None → env default.
    """
    cap = max_enrich if isinstance(max_enrich, int) and max_enrich > 0 else MAX_ENRICH
    try:
        instaloader, L = _make_instaloader(cookies_path)
    except Exception:
        # Enrichment is best-effort: if instaloader can't init, return the posts
        # without views (likes still present) rather than failing the whole walk.
        return posts, 0

    enriched = 0
    for p in posts:
        if not p.get("is_video"):
            continue
        if enriched >= cap:
            break
        try:
            post = instaloader.Post.from_shortcode(L.context, p["shortcode"])
            vv = getattr(post, "video_view_count", None) if post.is_video else None
            try:
                vp = post._field("video_play_count") if post.is_video else None
            except Exception:
                vp = None
            # Prefer play_count (the headline IG "plays" metric); fall back to
            # view_count, then to 0.
            p["view_count"] = int(vp or vv or 0)
            if not p.get("date"):
                p["date"] = post.date_utc.isoformat()
            if vv or vp:
                enriched += 1
        except Exception:
            # leave this post un-enriched (view_count stays whatever roster had / 0)
            pass
        time.sleep(ENRICH_SLEEP_S)
    return posts, enriched


def _iso(dateval):
    if not dateval:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(dateval, (int, float)):
        return datetime.fromtimestamp(dateval, tz=timezone.utc).isoformat()
    # gallery-dl emits "YYYY-MM-DD HH:MM:SS"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(dateval), fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return str(dateval)


def to_raw_video(p):
    """Map an enriched post dict to the RawVideo contract (matches tiktok_fetch)."""
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
    }


def run_user(req):
    handle = (req.get("handle") or "").strip()
    if not handle:
        _fail("instagram user mode requires a handle", "bad_request")
    # How many posts to enrich with views (the user's depth tier). The roster
    # walk must cover at least that many, so the enrich step has candidates.
    max_enrich = req.get("max_enrich")
    if not (isinstance(max_enrich, int) and max_enrich > 0):
        max_enrich = MAX_ENRICH
    limit = max(int(req.get("limit") or 30), max_enrich)

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
        roster, enriched = enrich_views(roster, cookies_path, max_enrich)
        videos = [to_raw_video(p) for p in roster if p.get("is_video")]
        _emit({"videos": videos, "enriched_views": enriched, "roster_size": len(roster)})


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
    elif mode == "session_check":
        run_session_check(req)
    else:
        _fail(f"unsupported instagram mode: {mode!r} (supported: user, session_check)", "bad_request")


if __name__ == "__main__":
    main()
