---
description: Find script/video templates and accounts to copy when the sources are UNKNOWN — three lanes (today's viral hits, cross-category UGC playbooks, direct competitors), ranked by remixability for the user's brand
argument-hint: "<your-brand> [one-line: what the brand does] [--region US]"
---

You are scouting TEMPLATE SOURCES for a brand whose copy-worthy accounts are not yet known. The deliverable: a ranked shortlist of videos + accounts worth remixing — [script] templates via `/ugcspy-rebrand`, [overlay] templates via `/ugcspy-decode` — each labeled with its lane and why it fits.

User arguments: `$ARGUMENTS` (the user's brand, optional one-line description, optional region; region defaults to US).

## Step 0 — you need to know what the brand does (and the language)

If no one-line description was given and you can't infer it from cached data, ASK the user one short question ("what does <brand> do, in one line?") before any discovery — lane phrases and every truthfulness judgment depend on it. Do not guess a niche from the brand name alone.

**Language defaults to ENGLISH.** Unless the user specifies a language/market, only English-language videos count as templates — verify with the transcript JSON's `language` field, not the caption (captions and audio often differ). Non-English candidates are FLAGGED ("wrong language — ask if you want the <xx> market") and never count toward the quota. If the user names a language or region market, that language replaces English as the filter.

## Phase A — cheap corpus passes (all lanes, ~1-2 min each, no approval needed)

Run these in this order — the trending pull comes first because its tag set powers the `bg` genericity discount on every later niche scan:

```bash
ugcspy discover <REGION> --trending --json        # ① lane 1+2: fetches + caches trend:<REGION> AND mines it — ONE pull
ugcspy discover "<niche phrase 1>" --json          # ② lane 3: the brand's own niche (derive 1-2 phrases from the description)
ugcspy discover "<adjacent niche>" --json          # ③ lane 2: same buyer, different product (fitness app → "language learning app", "budgeting app")
```

Do NOT also run `ugcspy trending` separately — `discover --trending` already fetches and caches the same rotating feed; a second pull wastes a fetch and mixes two different rotations into the `trend:<REGION>` cache.

Each `discover --json` returns `{corpus_size, cache_key, brands[], creators[]}` — aggregates, not videos. Candidate VIDEOS come later from `search --json` / `transcript --json`.

## Phase B — shortlist brands and accounts (judgment, zero cost)

From the combined `brands[]` evidence across all Phase A scans:
- Campaign codes (`campaignCodes` > 0) are near-proof of a run UGC program; `appVariant`/`authorMatch` mark brand-shaped tags; `background: true` marks generic topic tags. The table is evidence, not a verdict — YOU make the brand-vs-topic call.
- Consolidate tag families yourself (#pingo + #pingoai = one brand).
- From `creators[]`, note recurring accounts (the "accounts to watch" half of the deliverable).

Shortlist 2-4 candidate brands across lanes 2+3. STOP and confirm with the user before any deep pull: a full `ugcspy search` is ~5-8 min per brand. **Deep-search at most ONE brand without explicit approval for more.**

## Phase C — candidate videos (the only expensive phase)

**Lane 3/2 (the one approved brand):**

```bash
ugcspy search <brand-tag> --json                          # full roster; id + video_url + caption per row
ugcspy transcript <brand-tag> --top 3 --talking --json    # hooks + transcripts for its top talking videos
```

**Lane 1 (trending hooks — corpus already cached by Phase A):**

```bash
ugcspy transcript trend:<REGION> --top 8 --json           # classifies the TOP 8 cached hits, not the whole corpus
```

Report the coverage honestly ("classified 8 of N cached trending videos"). Don't pre-filter with `--talking` here — the NON-TALKING badge is how overlay candidates surface.

Always use `--json` for candidate-producing calls: the shortlist needs `id`, `video_url`, and `hook` fields, and the human-readable tables don't carry ids.

## Quota semantics — "top N" means N videos that FIT

When the user asks for "top 5 videos", the goal is **5 remixable videos that pass every condition** (language, class rules, honest brand-beat fit) — NOT the first 5 scanned:

- A scanned candidate that fails any condition is **FLAGGED with its reason and does NOT count** toward N.
- Keep scanning until you have N fits: continue down the ranked list, transcribe the next wave, pull the next corpus — in that order of cost.
- Tell the user the running score as you go ("3/5 fits, scanned 11").
- Stop early ONLY when the sources are exhausted or the next step would break a cost guardrail (e.g. a second unapproved deep-search) — then report the shortfall plainly: "found 3/5; lane 1 is dry today and filling the last 2 needs another brand deep-pull (~5-8 min) — want it?". A shortfall with a reason beats padding the list with unfit entries.

## Remixability judgment (applies to every candidate, all lanes)

Two template classes — label every proposal as one or the other:

**[script] — talking videos** (TALKING badge; transcript present). Usable only if:
- **The hook is product-independent.** The first line would survive with a different product in the video (rule out product demos where the product IS the format — /ugcspy-rebrand's FLAG case).
- **The brand beat is insertable/swappable** under /ugcspy-rebrand's iron rules: one beat, truthful for the user's brand, and — duration-aware — at or before the midpoint for >30s scripts.
Route: `/ugcspy-rebrand <video-id> <user-brand>`.

**[overlay] — non-talking videos** whose remixable asset is the ON-SCREEN TEXT sequence + format structure. The transcript tool CANNOT see overlay text (its hook field is caption-derived, not OCR) — so before ranking an overlay candidate, run `/ugcspy-decode <video-id>` to OCR the actual overlay narrative and confirm it's product-independent. If you shortlist one without decoding, label it **"unverified overlay candidate"** and do NOT quote an overlay line you haven't seen.
Route: `/ugcspy-decode <video-id>` first; remix needs a chosen source creator afterwards (`/ugcspy-remix <target-id> <source-id>` takes TWO videos — don't emit it with one).

**Either class** must also be **shootable** by a normal UGC creator (talking head, listicle voiceover, green-screen, b-roll montage). Be honest that cut-count/visual complexity is only verifiable via decode — flag, don't guess.

## Ranking rubric (apply in this order)

1. **Hook portability** — survives a product swap cleanly.
2. **Honest brand-beat fit** — a truthful insert exists under the rebrand rules (incl. the >30s midpoint cap).
3. **Shootability** for a normal creator.
4. **Evidence strength** — campaign codes / proven UGC program behind it beats a one-off hit.
5. **Views and freshness** — tiebreaks only.

## Output format

**1. Ranked template shortlist** (the user's quota of FIT videos, best first). **Every entry carries the SAME fields regardless of lane** — lanes 1 and 2 are not summaries; they get the full lane-3 treatment:

```
#N [lane 1|2|3] [script|overlay] @account — <views> — <duration>s — <language> — TALKING|NON-TALKING — <video_url>
   Hook: "<spoken hook verbatim>"           (script — from transcript --json)
   Overlay: "<opening overlay line>"        (overlay — ONLY after /ugcspy-decode; else "unverified overlay candidate")
   Original transcript: <full transcript text>
   Remix (target brand): <the full remixed script per /ugcspy-rebrand's rules, insert highlighted, with its position %>
   Why it fits: <one sentence tying format → the user's brand>
   Next: /ugcspy-rebrand <id> <user-brand>  (script)  |  /ugcspy-decode <id>  (overlay)
```

[script] fits MUST include the executed remix, not just the routing command — the deliverable is scripts the user can shoot, not homework. FLAGGED entries (listed separately, not numbered into the quota) show the same metadata + original transcript + the flag reason instead of a remix.

When the deliverable is a dashboard, use one uniform per-video section for ALL lanes — link, views, duration, language, talking badge, hook, original transcript, remix (or flag verdict) — identical to the lane-3 / creator-dashboard format.

**2. Accounts to watch** — recurring creators from `discover.creators[]` worth copying as ACCOUNTS (handle, videos-in-corpus, top views, lane, one-line rationale).

**3. The honest read** — one paragraph: which lane looks strongest for this brand right now, what you scanned and rejected (no silent dropping — if lane 1 was all sports clips today, say so), and any INCONCLUSIVE results: empty corpora may be relay failures not "no content" (a 0-video discover on an active niche is suspect — say so and suggest a retry), transcript scan caps reached, partial-platform search failures, batch transcription errors. Distinguish "scanned and rejected" from "failed to scan" everywhere.

## Cost expectations (tell the user upfront)

- Phase A: ~1-2 min per discover scan (3-4 scans total).
- Phase C: ONE brand deep-search ~5-8 min (never more without approval); transcripts ~10-40s per uncached video, batched, cached forever; each overlay decode ~30s.
- Filling a quota of N FIT videos usually takes several transcription waves (unfit candidates don't count) — report the running fits/scanned score between waves.

## What NOT to do

- Don't propose non-talking videos as SCRIPT templates — there are no spoken words for /ugcspy-rebrand to remix. Propose them as [overlay] candidates routed through /ugcspy-decode, or not at all.
- Don't quote overlay lines you haven't decoded — the transcript tool can't see on-screen text.
- Don't treat the brand-candidates table as truth — it's structural evidence; you make the brand-vs-topic call.
- Don't run `ugcspy trending` AND `discover --trending` in the same session — one rotating-feed pull, used for both.
- Don't deep-search more than one candidate brand without explicit user approval.
