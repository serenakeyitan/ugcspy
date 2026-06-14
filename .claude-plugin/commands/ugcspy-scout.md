---
description: Organize a brand's hand-vetted set of creators into a mineable library — profile their shared style signature, then lay out each creator's remixable videos (talking, on-language, real hook) ready for /ugcspy-rebrand. Seed-expansion only — NO auto-discovery, so no false fits.
argument-hint: "<your-brand> [one-line: what the brand does] <seed @handles or video links...>"
---

You are organizing a brand's **already-chosen** creators into a long-term mining library. The user hands you a SEED SET of creators they've **already vetted by eye** — they've watched these accounts and confirmed the style fits. Your job is NOT to find new creators; it's to (1) name the shared signature so the brand can brief shoots against it, and (2) pull each seed's roster and surface the videos worth remixing, each ready to hand to `/ugcspy-rebrand`.

**Why no discovery.** Earlier versions tried to auto-find "similar" creators via a follow-graph and keyword corpora. Both failed the same way: the thing that actually decides whether a creator fits a brand's style is **largely visual** (a clean talking-head vs a voiceover over movie/B-roll clips vs a photo carousel), and none of the tools here can see the screen — they only get audio + text. So auto-discovery confidently promoted creators whose *words* matched but whose *format* was wrong (e.g. a therapist narrating over HBO scene cuts). A confident false fit is worse than no fit, because you have to catch it by eye anyway. This version removes that failure mode entirely: it only operates on creators YOU already eyeballed. The `ugcspy similar` / `ugcspy discover` / `ugcspy trending` commands still exist if you ever want raw discovery by hand — they are deliberately NOT part of this skill.

User arguments: `$ARGUMENTS` (brand, optional one-line description, and the seed creators — handles or pasted TikTok URLs).

## Step 0 — brand, language, seeds

1. **What does the brand do, in one line?** Needed for the `/ugcspy-rebrand` step later (the brand beat must be truthful). If absent and not inferable, ask.
2. **Language defaults to ENGLISH** — verify per video with the transcript JSON's `language` field (not the caption). Non-English videos are flagged and not surfaced as remixable unless the user named that market.
3. **You need ≥1 seed creator.** If the user has none to give, this skill is the wrong tool — tell them so plainly: there is no auto-discovery here; they need to pick creators they like first (browse TikTok, save a few accounts whose style fits), then come back. Do NOT silently fall back to keyword guessing.

Normalize each seed: a pasted URL like `tiktok.com/@x/video/123` → the handle `@x`.

## Step 1 — profile the shared signature (descriptive, not a filter)

Pull a fresh windowed sample + transcribe a bounded top-cohort per seed. The flags matter — defaults silently mismatch (see each comment):

```bash
ugcspy search @<seed> --platform tiktok --days 90 --limit 100 --refresh --json   # --refresh: else it reads cache and misses new posts; --platform tiktok: else it mixes in Instagram; --limit 100 is VIEW-SORTED (top sample, not "typical")
ugcspy transcript @<seed> --platform tiktok --days 90 --top 12 --json             # same 90d window as search (transcript defaults to 30d!); top 12 by views; do NOT pass --talking here (it would hide the non-talking misses you must show)
```

Window/platform alignment is not optional: `search` defaults to `--platform all` and skips fetching when cache exists; `transcript` defaults to 30 days and TikTok-only. Left at defaults, the reach band, the signature, and the remix list would each describe a *different* set of videos. Pin `--platform tiktok --days 90` on both, `--refresh` on search.

You're reviewing the seed's **top 12 videos by views** (state that coverage explicitly). If that yields fewer than ~5–8 remixable talking videos, raise `--top` and re-pull (transcripts cache, so it's cheap) — but keep every reviewed video's classification, misses included.

From the seeds together, name the **shared signature** on the axes the tools CAN actually read (audio + text only):
- **Speech-led vs not** (`talking` field — means the audio has ≥8 lexical words; it does NOT mean the speaker is on screen).
- **Script structure** — listicle / monologue / explainer / story / reaction (from the transcript text).
- **Typical length band** (`duration_sec`).
- **Textual voice/wording** — what the *words* sound like (clinical, casual, punchy, reflective), inferred from the transcript. NOT delivery tone — the tools expose no prosody/pacing/volume, so don't claim "calm" or "hype" as if verified.

State it plainly: *"Your set shares: speech-led 30–60s explainers that name a mechanism then resolve it, clinical wording."* This is a **description of what the user already picked**, to brief future shoots — not a gate you apply to anyone.

**Be explicit about the visual axis.** Speaker-on-screen vs voiceover-over-footage vs carousel is a VISUAL property the tools CANNOT verify — `talking=true` says nothing about what's on screen. It was the user's call when they vetted these seeds, and it stays their call. Never describe a creator as a "talking-head" from the data; say "speech-led" and leave the visual to the user.

If the seeds split into two clear sub-styles, say so and group the output by sub-style.

## Step 2 — lay out each seed's remixable videos

From the **reviewed cohort** (the seed's top 12 by views you transcribed in Step 1), split every video into remixable vs not — the remixable ones are usable with `/ugcspy-rebrand`:
- **speech-led** (`talking = true` — there's a script; non-talking/0-word videos are [overlay] territory, listed under the seed as "overlay/decode only", not dropped),
- **on-language** (English by default, per the `language` field),
- **has a real hook** (a substantive opening line, not a 3-word fragment or a music-bed artifact).

Because the cohort is bounded (top 12 reviewed), the misses list is complete *for that cohort* — say "of the top 12 reviewed, N are remixable". If fewer than ~5–8 are remixable, raise `--top` and re-pull rather than quietly under-delivering. Every reviewed video appears somewhere — remixable or with its miss reason.

**If a seed's `search` returns `[]`:** the CLI cannot tell a genuinely empty roster from a failed yt-dlp walk (it returns `[]` on timeout too). Do NOT assert "this creator has no videos" — report it as **"empty or failed pull — the CLI can't distinguish; retry, or check the handle"**.

## Output format — a mining library, grouped by seed

For the whole set, lead with the shared signature, then one block per seed:

```
SHARED SIGNATURE: <one line — the common thread (speech-led / structure / length / wording)>
  (visual format — speaker-on-screen vs voiceover/clips — was your call; tools can't verify it.)

── @seed_a — <top-sample reach band> · <its signature if it differs from the set> ──
  (reviewed top 12 by views; "reach band" is the top sample, not necessarily typical)
  Remixable videos:
    <video_url>  · <views> · <duration>s · <lang> · hook: "<hook verbatim>"
       → /ugcspy-rebrand <video-id> <user-brand>
    <video_url>  · ...
       → /ugcspy-rebrand <video-id> <user-brand>
  Not remixable (shown, not hidden):
    <video_url>  · non-talking (overlay/decode only)
    <video_url>  · wrong language (<xx>)

── @seed_b — ... ──
  ...
```

EVERY video carries its **clickable TikTok link** and, when remixable, the exact `/ugcspy-rebrand` command. The deliverable is "here are your vetted creators' mineable videos, organized and ready to remix" — nothing claimed beyond what audio+text verifies.

## The honest read

Close with: how coherent the seeds' shared signature was, any seed whose roster came back thin or failed to pull (a tikwm walk on a big account can time out and return `[]` — say "failed to fetch", not "no videos"), and how many remixable videos the set yields total. If a seed turned out to be off-signature from the rest (you'll see it in the transcripts), flag it — the user may have mis-tagged it — but DON'T drop it; it's their pick.

## What NOT to do

- **No auto-discovery.** Don't run `ugcspy similar`, `ugcspy discover`, or `ugcspy trending` from this skill, and don't keyword-guess "similar" creators. If the user has no seeds, say so — don't invent a seed set.
- **Don't claim visual format.** Talking-head vs voiceover-over-footage vs carousel is unverifiable here; it was the user's eyeball call. Never assert a creator is a clean talking-head from transcript alone.
- **Don't hide a seed's misses.** Non-talking / wrong-language / weak-hook videos are listed under the seed with the reason, so the picture is complete.
- **Don't rank seeds against each other or drop one.** They're all user-chosen; organize, don't judge. Flag an apparent off-signature seed, but keep it.
- **Don't report a failed roster pull as "no videos."** Distinguish an empty result (creator genuinely posted nothing in window) from a walk timeout/relay failure.
