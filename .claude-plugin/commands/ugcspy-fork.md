---
description: Generate a creator brief from a competitor video (uses your Claude Code subscription, no API key)
argument-hint: "<video-id-from-search-results | video-url>"
---

The user wants a creator brief from a competitor video. The standalone `ugcspy` CLI does NOT have a `fork` command — brief generation lives in this plugin so the user's Claude Code subscription does the LLM work (no Anthropic API key needed, no per-brief cost).

User arguments: `$ARGUMENTS`

## Step 1 — Look up the video

Run `ugcspy search --json` against the cached DB and find the matching video. Two cases for `$ARGUMENTS`:

- **Numeric id** (e.g. `1`, `42`): the user got it from a previous `/ugcspy-search` table. Pull the latest cached search and find the row by id.
- **Video URL** (e.g. `https://www.tiktok.com/@glossier/video/...`): match against the `video_url` field.

Use Bash:
```bash
sqlite3 ~/.ugcspy/db.sqlite "SELECT json_object('id', id, 'platform', platform, 'video_url', video_url, 'caption', caption, 'view_count', view_count, 'like_count', like_count, 'posted_at', posted_at, 'hook_text', hook_text, 'author_handle', author_handle) FROM videos WHERE id = $ARGUMENTS OR video_url = '$ARGUMENTS' LIMIT 1;"
```

Parse the JSON. If empty, tell the user to run `/ugcspy-search <brand>` first so the video is in cache.

Note: `author_handle` may be set (the actual creator who posted the video, varies per row in hashtag-mode results) or null (legacy data). Use it in the brief: a third-party UGC video by `@growwithnomes` with `#befreed` is a different storytelling reference than the brand's own post.

## Step 2 — Generate the brief in this chat

You (Claude Code) write the brief directly — do NOT shell out to any binary. Use the video data above to produce a markdown document with these exact sections, in order:

```
# Brief: <punchy title>

## Hook variations
1. <≤90 chars, written for first-2-second retention>
2. <alternative hook>
3. <alternative hook>

## Format
<Pick ONE: GRWM | POV | talking_head | product_demo | unboxing | tutorial | before_after | voiceover_broll | duet_stitch | other>
<One-line note on why this format works for the source video>

## Beat sheet
1. 0:00-0:03 <one-sentence beat>
2. 0:03-0:08 <one-sentence beat>
3. 0:08-0:14 <one-sentence beat>
4. 0:14-0:20 <one-sentence beat>

## Suggested b-roll
- <3-5 bullets, each one concrete shot idea>

## CTA
<One line the creator can drop in the last 2 seconds>
```

Write the brief inline in the chat response. Be concrete, not generic. Pull from the actual caption and metrics. No filler.

## Step 3 — Offer to save

After showing the brief, ask the user if they want it saved to a file:

```bash
mkdir -p ~/.ugcspy/briefs && cat > ~/.ugcspy/briefs/brief-<platform>-<external_id>.md
```

Default to NOT saving unless asked — the user can copy/paste from the chat if that's all they need.
