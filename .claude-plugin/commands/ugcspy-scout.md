---
description: Find CREATORS worth copying long-term for a brand — start from the brand's ideal creators (or build that seed set with them), then surface more creators in the same FORMAT/STYLE, ranked by signature-fit and reach. Creator-centric, not one-off-video-centric.
argument-hint: "<your-brand> [one-line: what the brand does] [seed @handles or video links] [--region US]"
---

You are scouting CREATORS for a brand to copy as a **long-term well**, not single viral videos to remix once. The deliverable is a ranked shortlist of **creators** whose body of work fits the brand's format/style, each with sample video links so the user can eyeball them and adopt the ones that fit. Once chosen, the user mines those creators' videos every week via `/ugcspy-rebrand`.

This is **creator-centric**. The old content-centric scout asked "is THIS video remixable?" and surfaced structurally-portable videos that were tonally wrong for the brand (a marriage monologue, a period-education clip). The fix: anchor on the **kind of creator** the brand wants, and rank by similarity to that, not by raw virality.

User arguments: `$ARGUMENTS` (brand, optional one-line description, optional seed handles/links, optional region; region defaults to US).

## Step 0 — brand, language, and the SEED SET (the whole thing pivots on this)

1. **What does the brand do, in one line?** If not given and not inferable from cached data, ASK — every truthfulness/fit judgment depends on it. Don't guess a niche from the brand name.

2. **Language defaults to ENGLISH.** Unless the user names a language/market, only English-language creators count — verify with the transcript JSON's `language` field, not captions. Non-English creators are FLAGGED ("wrong language — ask if you want the <xx> market"), never counted. **Language is the real market filter here — `--region` is NOT enforced by the tools** (keyword/account/transcript calls don't filter or report a creator's region). Treat `--region` only as the trending-feed region; for market targeting, lean on the language filter and say so, rather than implying region is applied.

3. **Ask for the seed set — the ideal creators to copy:**

   > "Do you have creators whose style you'd want more of? Drop their @handles or some video links. If not, I'll show you options grouped by style and you pick."

   - **They have seeds → Path A** (find more creators like these).
   - **They don't → Path B** (build the seed set with them first, then Path A).

This question is not optional. Without a seed set you're back to content-centric scouting — the exact problem this version exists to fix.

---

## Path A — "find more creators like these"

The seed set defines a **target signature**. You find more creators matching that signature, primarily through the seeds' follow-graph, falling back to corpus when the graph is thin (it usually is — see the caveat).

### A1 · Profile the seed set → the target signature

Pull each seed's roster and recent talking videos; characterize the set on **format/style axes ONLY** (topic is recorded, NOT used as a filter — the brand beat re-aims topic):

```bash
ugcspy search @<seed> --days 90 --limit 100 --json   # windowed roster sample: views, durations, captions
ugcspy transcript @<seed> --top 4 --json             # talking/non-talking, structure, length, tone
```

`search` returns a **windowed sample** (here: ≤100 of the last 90 days, sorted by views — NOT a literal full roster, and it over-samples their hits). `transcript --top N` transcribes their N TOP-VIEW cached videos (not "recent"). Both are enough to read a signature; just don't describe them as the complete catalog.

Derive the signature on these axes — all of which `search`/`transcript` JSON actually expose:
- **Talking vs non-talking** (the hard split — `talking` field; script creators vs overlay creators).
- **Script structure** — listicle / monologue / story / explainer / reaction (read from the transcript text).
- **Typical length band** — e.g. 15–35s, 60–90s (`duration_sec`).
- **Tone** — calm / hype / deadpan / warm (read from the transcript).
- **Topic** — record it for context, but DON'T filter on it.

**Faceless-vs-face is NOT derivable from these tools** (no JSON field reports visual presentation). Don't guess it as a ranking axis — if it matters to the user, have them eyeball the sample links. Note it as an observation only when a thumbnail makes it obvious.

Write the signature down explicitly ("the seed set is: calm 30–60s talking-head psychology/advice monologues"). It's the rubric for everything downstream.

If seeds span two distinct signatures (e.g. some listicles, some monologues), say so and treat them as two target signatures — rank within each.

### A2 · Graph pass — the follow-graph (fire it FIRST, it's one fast call)

Run this BEFORE A1's seed profiling — it's a single fast call, and with several seeds the profiling roster-pulls + transcripts take minutes; don't make the cheap graph wait behind them. (Kick it off, then profile while it runs.)

```bash
ugcspy similar @<seed1> @<seed2> @<seed3> ... --json   # handles OR pasted TikTok URLs both work
```

Returns `{seeds, count, creators:[{handle, seedsFollowing, cachedMaxViews}], seedResults:[{handle, status}]}`. `seedsFollowing` = how many seeds follow that candidate; a creator multiple seeds independently follow is a strong cluster signal.

**Report the hit-rate from `seedResults`** — `status` is the seed's follow-count, or `-1` (blocked/unreadable list) or `-2` (handle didn't resolve). Say it plainly: "3 of 8 seeds had readable following lists; 5 were blocked." This is how you distinguish a thin-but-working graph from one the relay simply refused.

**CAVEAT — the graph is usually thin, and that's expected.** tikwm's following endpoint is private/blocked for ~60% of creators, and seeds from different sub-niches share few followings, so a typical run returns many creators all at `seedsFollowing: 1` — or nearly nothing, with most seeds `status: -1`. **Do not treat the follow-graph as a ranker.** It's a discovery NET: it surfaces handles the brand might recognize. Real ranking comes from A4 (signature-fit + reach).

Multi-seed candidates (`seedsFollowing ≥ 2`) are the gold here — prioritize verifying those — but never *depend* on the graph alone; when the hit-rate is low (it usually is), A3's corpus pass carries the work.

### A3 · Corpus pass — when the graph is thin (it usually is)

Cast format/style-matched keyword nets to surface CANDIDATE CREATORS, then keep only those whose own body of work matches the seed signature. Two tools, different payloads — use both:

```bash
ugcspy discover "<niche phrase in the seed's format>" --json   # {brands[], creators[]} AGGREGATES — recurring creators, capped (~top 15), one-off authors excluded
ugcspy search "<niche phrase>" --mode keyword --platform tiktok --limit 60 --json   # the actual CORPUS VIDEOS, each with author_handle + video_url + views
```

`discover --json` gives you the *recurring* creators in the niche (a good starting handful) but NOTHING per-video and it drops authors who appear once. To get the full author pool with their videos — the candidates you actually rank — use `search --mode keyword`, then group its rows by `author_handle`. Pick niche phrases dense in the seed's FORMAT (a monologue-advice seed → "advice that changed my life", "harsh truths"; a listicle seed → "things to do alone", "glow up tips"). Topic can flex — match how the content is made, not what it's about. Gather candidate handles from both, then verify each one's signature in A4.

### A4 · Rank candidate creators by SIGNATURE FIT (the real ranking)

For each candidate (from graph and/or corpus), pull a windowed sample of their work — enough to read consistency, not one video:

```bash
ugcspy search @<candidate> --days 90 --limit 100 --json   # windowed sample (≤100 of last 90d, view-sorted)
ugcspy transcript @<candidate> --top 3 --json             # does their style match the seed signature?
```

(This is a sample, not the literal full catalog — it's view-sorted so it over-represents their hits. Judge consistency as "of the sampled videos, how many clear a real-reach bar," not "every video they ever posted.")

Rank by, in this order, with concrete thresholds so two runs agree:
1. **Signature match (gate, then rank).** A candidate must match on the HARD axis — talking vs non-talking — or it's out (a non-talking creator can't be a [script] source for a talking seed). Among those, score the soft axes: structure match, length-band overlap (their median duration within ~±50% of the seed band), tone match. Topic is NOT scored. A calm monologue creator matches a calm monologue seed on a different topic.
2. **Consistency.** Of the sampled videos, what fraction clear a real-reach bar (default ≥500K, or ~the seeds' own median if lower)? **STRONG ≥ 60%, MODERATE 20–60%, one-hit < 20%.** Higher is better — a reliably-performing creator is the goal. (This is the inverse of the old 100× rule — see below.)
3. **Reach.** Median sampled views in roughly the seeds' band (view floor is a soft signal, not a hard gate).
4. **Graph adjacency** — `seedsFollowing` as a tiebreak only, never primary.
5. **Shootability** — the brand's own creator could plausibly make this (talking head, listicle voiceover, green-screen — not a 50-cut edit or a stadium shoot).

Language: a candidate counts only if its sampled videos are predominantly in the target language (default English — **≥ ~70% English** by the transcript `language` field). Mixed/other-language creators are flagged, not ranked.

### The 100× outlier rule is DEMOTED in creator-centric mode

The 100× rule ("a video must beat the creator's prior by 100×") answered "did the SCRIPT outperform the ACCOUNT?" — the right question when hunting a one-off video to remix. It is the **wrong** question for picking a creator to copy long-term: a creator whose videos *consistently* do well — exactly what 100× *rejected* as "account-powered" — is precisely who you want, because consistency means their format reliably works.

So: **100× is NOT a gate here.** Use **consistency + signature-fit + reach** instead. Keep 100× only as an optional ANNOTATION on a sample link ("this one was a breakout: 3.9M vs their typical 12K = 325×") — useful color, never a filter.

---

## Path B — "I don't know who to copy" → build the seed set

List candidate creators for the user to choose from, **grouped by FORMAT/STYLE** (the same axes A1 profiles — so the menu and the matcher speak one language). Then the chosen creators become the seed set and you run Path A.

1. Discover broadly in the brand's space (a few `ugcspy discover "<niche>" --json` over phrases adjacent to what the brand does).
2. Profile the recurring creators (light: one roster pull + one transcript each to get their signature).
3. **Present them grouped by style**, each group a format archetype, each creator with 2–3 sample **clickable** video links + their typical reach:

   ```
   ── Calm faceless monologues (psychology / advice) ──
     @creator_a — typical 200K, 30–60s talking head
        https://tiktok.com/@creator_a/video/...   "It has to be perfect, so I'll do it later..."
        https://tiktok.com/@creator_a/video/...
     @creator_b — ...
   ── Listicle voiceovers (self-improvement) ──
     @creator_c — ...
   ── Story-driven / green-screen ──
     ...
   ```

4. The user picks the creators (and/or whole groups) that fit. Those become the seed set → **run Path A** to expand around them.

If the user is happy with just the menu (doesn't want expansion), the grouped list IS a usable deliverable on its own — but offer Path A expansion.

---

## Output format — RANKED CREATORS (not ranked videos)

**1. Ranked creators to copy** (the user's quota, best first). Each entry:

```
#N @creator — <typical reach band> — <signature: talking|overlay · structure · length · tone>
   Why it fits: <one sentence: which seed-signature axes it matches>
   Source: follow-graph (seedsFollowing N) | corpus "<niche>" | both
   Sample videos (clickable):
     <video_url>   "<hook or opening overlay line>"   [breakout: <ratio> if notable]
     <video_url>   "<hook>"
     <video_url>   "<hook>"
   Copy path: /ugcspy-rebrand <video-id> <user-brand>   (per sample, for the talking ones)
```

EVERY listed video carries its **clickable TikTok link** — fits, samples, wildcards, all of them. (This was the gap in the old output.)

**2. Wildcards — off-archetype but big** (clearly separated, optional inspiration). Creators NOT matching the seed signature but pulling large numbers in an adjacent space. One line each + reason + **link**, so the user can glance and ignore or not. Never mixed into the ranked seed-matches.

```
~ @offbrand_creator — 6M, but story-skit format (off your monologue signature) — https://tiktok.com/...
```

**3. The honest read** — one paragraph: how coherent the seed signature was, how the follow-graph did (hit-rate, thin/empty is normal), whether ranking came mostly from graph or corpus, and what you scanned-and-rejected vs failed-to-scan. Distinguish "no similar creators found" from "the follow-graph was blocked" — never report a tooling failure as an empty result. A 0-result `discover` on an active niche is suspect (likely a relay blip) — say so and retry.

## Quota semantics — "top N" means N creators that FIT the signature

Default quota is **5 fitting creators**. A scanned candidate that fails the signature match (wrong format, wrong tone, inconsistent, off-language) is FLAGGED and does NOT count — keep scanning (more graph seeds, more corpora, the next wave of candidate roster-pulls) until you have N. Tell the user the running score ("3/5 fitting creators, scanned 9").

**Hard budget so the hunt can't run away** (the candidate pool can be sparse, and each candidate costs a roster pull + ~3 transcriptions). Default ceilings, all user-overridable:
- **≤ 8 corpus scans** (`discover` / `search --mode keyword`),
- **≤ 30 candidate creators verified** (roster pull + transcripts),
- **≤ 60 transcription calls** total.

When you hit the quota → stop and report. When you hit a budget ceiling before the quota → **STOP and report the shortfall** with what you found, what's left to try, and the cost of continuing — don't silently grind past it. "Sources exhausted" means: the format-matched corpora are returning only already-seen or non-matching creators. A shortfall with a clear reason beats an unbounded loop or a padded list.

## Cost expectations (tell the user upfront)

- Seed profiling (A1): one roster pull + one transcript per seed, ~30–60s each.
- Graph pass (A2): `ugcspy similar` is one fast call (~10–30s); often thin.
- Corpus pass (A3): ~1–2 min per `discover` scan.
- Candidate verification (A4): one roster pull + ~3 transcripts per candidate you rank — the dominant cost. Verify candidates in priority order; don't pull every corpus author.
- A full `ugcspy search` on a very active creator can run several minutes — pull the shortlist, not everyone.

## What NOT to do

- Don't rank on raw virality or the follow-graph score — rank on **signature fit + consistency + reach**.
- Don't treat the follow-graph as authoritative — it's a thin net (~60% of following lists are blocked); pair it with corpus matching and report the hit-rate.
- Don't filter candidates by TOPIC — match FORMAT and STYLE; the brand beat re-aims topic.
- Don't apply the 100× rule as a gate here — consistency is a virtue for a long-term creator, not a disqualifier. 100× is an optional breakout annotation only.
- Don't list a video without its clickable link — every video, every section.
- Don't mix off-archetype wildcards into the ranked seed-matches — separate section, clearly labeled.
- Don't propose non-talking creators as [script] sources — they're [overlay]/decode creators; label the class.
- Don't report a relay failure or blocked graph as "no creators found" — distinguish scanned-and-rejected from failed-to-scan.
