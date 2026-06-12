---
description: Find script/video templates and accounts to copy when the sources are UNKNOWN — three lanes (today's viral hits, cross-category UGC playbooks, direct competitors), ranked by remixability for the user's brand
argument-hint: "<your-brand> [one-line: what the brand does] [--region US]"
---

You are scouting TEMPLATE SOURCES for a brand whose copy-worthy accounts are not yet known. The deliverable: a ranked shortlist of videos + accounts worth remixing (via `/ugcspy-rebrand`), each labeled with its lane and why it fits.

User arguments: `$ARGUMENTS` (the user's brand, optional one-line description, optional region).

## The three lanes (run all three, cheapest-signal first)

**Lane 3 — direct competitors (流量保底, run FIRST).** The user's niche has brands already doing UGC; their proven scripts are the safest templates.

```bash
ugcspy discover "<niche phrase derived from what the brand does>" --json
```

Derive 1-2 niche phrases from the brand description (e.g. an AI reading app → "ai learning app", "book summary app"). Read the `brands` table: campaign codes (`Codes` column) are near-proof of a run UGC program; `acct`/`app` signals mark brand-shaped tags; `bg` marks generic topic tags. YOU judge which rows are real brands — the table is evidence, not a verdict. Consolidate tag families yourself (#pingo + #pingoai = one brand). For each real competitor brand found:

```bash
ugcspy search <brand-tag>        # their full UGC roster, ranked by views
```

**Lane 2 — cross-category playbooks (套用 hooks from other verticals).** Hook formulas transfer across categories; a viral UGC structure from a non-competing product can be re-aimed at the user's brand without competing for the same audience.

- Mine the trending corpus for UGC-program brands in OTHER categories: `ugcspy discover <REGION> --trending --json`
- Also run `ugcspy discover` on 1-2 ADJACENT-but-different niches (same audience, different product — e.g. the user's brand is a fitness app → scout "language learning app", "budgeting app": same self-improvement buyer).
- For each cross-category brand with strong evidence, pull their top talking videos and judge HOOK PORTABILITY: does the hook formula survive with the product swapped? ("Psychology shows your X reveals Y" ports anywhere; "watch me unbox this" doesn't.)

**Lane 1 — today's viral hits (蹭热度, opportunistic).** Network-wide heat, mostly NOT brand content — the lowest-precision lane, but the only one that catches today's wave.

```bash
ugcspy trending <REGION>                      # default US; cached as trend:<REGION>
ugcspy transcript trend:<REGION> --talking --top 5
```

Judge every hit for remixability before proposing it (rules below). Talking hits are script templates; high-fit NON-talking hits (overlay-text montages over a trending sound) are format templates — keep those too, routed to the decode path. Discard what's neither: sports clips, news moments, and meme formats with no text narrative. Finding 1-2 genuinely remixable trend formats is a good day.

## Remixability judgment (applies to every candidate, all lanes)

Two template classes — label every proposal as one or the other:

**[script] — talking videos** (`--talking`; transcript present). Usable only if:
- **The hook is product-independent.** The first line would survive with a different product in the video (rule out product demos where the product IS the format — /ugcspy-rebrand's FLAG case).
- **The brand beat is insertable/swappable** under /ugcspy-rebrand's iron rules: one beat, truthful for the user's brand, and — duration-aware — at or before the midpoint for >30s scripts.
Route: `/ugcspy-rebrand <video-id> <user-brand>`.

**[overlay] — non-talking videos** whose remixable asset is the ON-SCREEN TEXT sequence + format structure (overlay-text montage over a sound). Usable only if the overlay narrative is product-independent the same way a spoken hook would be. The brand insert is an overlay caption beat, not a spoken sentence.
Route: `/ugcspy-decode <video-id>` (OCRs the overlay narrative) then `/ugcspy-remix`.

**Either class** must also be **shootable** by a normal UGC creator (talking head, listicle voiceover, green-screen, b-roll montage) — not a stadium event or a 50-cut edit.

## Output format

A single ranked shortlist (aim for 5-10 entries, best first), each entry:

```
#N [lane 1|2|3] [script|overlay] @account — <views> — <link>
   Hook: "<spoken hook verbatim>"            (script) — or the opening overlay line (overlay)
   Why it fits: <one sentence tying format → the user's brand>
   Next: /ugcspy-rebrand <video-id> <user-brand>     (script)
   Next: /ugcspy-decode <video-id>                    (overlay)
```

Then a one-paragraph read on which lane looks strongest for this brand right now, and anything you scanned and rejected that the user might expect to see (no silent dropping — if lane 1 was all sports clips today, say so).

## Cost expectations (tell the user upfront)

- Lane 3 niche scans: ~1-2 min each. A competitor's full `search` is ~5-8 min for an active brand — ask before running more than one.
- Transcriptions: ~10-40s per uncached video, batched; cached are instant.
- Lane 1 trending pull: ~30-60s.

## What NOT to do

- Don't propose non-talking videos as SCRIPT templates — they have no spoken words for /ugcspy-rebrand to remix. Propose them as [overlay] format templates routed to /ugcspy-decode, or not at all.
- Don't treat the brand-candidates table as truth — it's structural evidence; you make the brand-vs-topic call.
- Don't run a full `ugcspy search` on every candidate brand — shortlist first, confirm with the user, then deep-pull.
