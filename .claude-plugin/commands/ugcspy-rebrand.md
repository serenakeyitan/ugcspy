---
description: Rebrand a UGC video transcript to a target brand with MINIMAL edits — never touch the hook, only swap/insert the promotion, smoothly and truthfully
argument-hint: "<video-id-or-tiktok-url> <target-brand> [one-line: what the brand does]"
---

You are rebranding ONE UGC video transcript so it promotes a target brand instead of — or in addition to — whatever it promoted before. The output is a script a creator can re-shoot or re-voice. The viewer should finish the video and want to search the brand name; the video must NOT become an ad.

User arguments: `$ARGUMENTS` (video id or URL, target brand, optional one-line brand description).

## The iron rules (violating any of these is a failed run)

1. **The hook is untouchable.** Not one word, not punctuation, not emphasis. The hook is the video's first spoken line (ugcspy's `hook` field). If you are unsure where the hook ends, treat the entire first sentence as frozen.
2. **Minimal change.** Every word outside the promotion beat stays byte-identical to the source. You are not editing for style, grammar, flow, or length anywhere else — even if the transcript has ASR noise, leave it; the creator knows what they said. If you find yourself rewriting a sentence that has no brand in it, stop.
3. **One promotion, not a takeover.** The brand appears at ONE beat (two only if the source had two promotional beats). The video's subject stays the content; the brand is a supporting detail the way real UGC drops it.
4. **Truthful to the target brand.** The inserted claim must describe something the brand actually does. Never inherit the old product's claims wholesale — adapt them to what the target brand really is.
5. **Searchable.** Say the brand NAME clearly once, in a form a viewer can type into a search bar. Spelled-out name beats a vague "this app".
6. **Never the final beat — and placement is duration-aware.** The video's own closer ALWAYS comes after the brand line; the last thing the viewer hears is the creator's voice, never the brand. Where the line lands depends on script length, because retention decides who ever hears it:
   - **≤30s scripts**: 靠后或者中间 (mid-to-late) is fine — short videos hold most viewers to the end.
   - **>30s scripts**: drop-off makes late beats worthless — the brand line must land **no later than the midpoint** (中间或更早, roughly 30–50% in). A beautiful insert at 80% of a 60-second video converts nobody.
   - If a long script's only honest host beat sits past the midpoint, FLAG the video instead of forcing a late insert — recommend a shorter video in the same content family instead.
   - In list-format videos, grafting onto a mid-list item (e.g. "I did that one on <brand> first" after book #3 of 5) is allowed and usually the cleanest way to hit the midpoint.

## Step 1 — Get the transcript

```bash
ugcspy transcript <id-or-url> --json
```

Cached transcripts return instantly. If transcription fails, relay the error verbatim (it names the fix). Use the `hook` and `transcript` fields; the hook string is your frozen first line.

## Step 2 — Learn how the target brand is REALLY pitched

Do not invent marketing copy. In priority order:

1. **The brand's own UGC corpus in the local cache** — `ugcspy transcript <brand> --top 3 --talking --json` (or `ugcspy search <brand> --json` captions). Real creators' pitch lines tell you the brand's true function AND the native register for dropping it — real creators write things like "it's 2026, just use an app like <brand>" or "the app is called <brand> btw"; that offhand register, not ad copy, is what you're matching.
2. **The user's one-liner argument**, if they gave one.
3. **Ask the user** one short question: "what does <brand> do, in one line?" — only if 1 and 2 both came up empty.

## Step 3 — Find the promotion beat (judgment, not pattern-matching)

Read the transcript and decide what kind of video this is:

- **SWAP case — it already promotes something.** A promotion is any beat whose job is to make the viewer aware of or want a named product, service, method, or "comment X and I'll send it" funnel. Replace that beat's product with the target brand, adapting the claim to rule 4. Keep the beat's position, length, and sentence rhythm as close to the original as possible.
- **INSERT case — no promotion.** Find the single most natural host moment: the place where the creator explains *how* they do the thing, *what* they use, or transitions toward a takeaway. Insert one short brand sentence (or graft a clause onto an existing sentence) that grows out of the content right before it. The transition must reuse the video's own vocabulary and voice — if the creator says "bro", the insertion can say bro; if the creator is clinical, stay clinical.
- **FLAG case — the brand can't live here honestly.** If the product IS the format (the video is a demo of the original app — e.g. an AI-tutor conversation where the product is the other voice), or the topic has no truthful bridge to the brand, say so plainly. Offer the least-stretchy option you can, explicitly labeled as a stretch, and let the user decide. Do not force it — a forced insert reads as an ad and defeats the purpose.

Placement instinct: real UGC drops the brand right after a value moment (the tip that worked, the result shown), not at the top and not as a closing tagline. The hook's job is retention and it already exists — leave it alone (rule 1). Per rule 6, the creator's own closer must follow the brand line, and on >30s scripts the line must sit at or before the midpoint — when in doubt, slot the insert one beat EARLIER than feels natural, never later. Check the video's duration (the transcript JSON carries `duration_sec`) before choosing the beat.

## Step 4 — Output format

Present exactly three things:

**1. The rebranded transcript** — full text, ready to shoot/voice.

**2. The diff** — only the changed region, in this form (everything not shown is byte-identical):

```
− <the removed words, if any>
+ <the added words>
```

**3. Placement note** — one sentence: where the brand landed and why that beat.

If useful for the search-up goal, add one line of suggested caption tags (`#<brand>` + the video's existing topical tags) — the caption is how ugcspy-style tools and viewers find brand UGC.

## Step 5 — Self-check before presenting (silent, mandatory)

- [ ] Hook byte-identical to the source `hook` field?
- [ ] Outside the promotion beat, is every word unchanged?
- [ ] Is the brand named once, clearly, searchably?
- [ ] Does the claim match what the brand actually does (per Step 2 evidence)?
- [ ] Read the transition sentence aloud in the creator's voice — does it sound like them, not like a sponsor read?
- [ ] Is the video still about its subject, with the brand as a detail?
- [ ] Is there at least one beat of the creator's OWN content after the brand line? (The script must never end on the brand.)
- [ ] If `duration_sec` > 30: does the brand line sit at or before the script's midpoint? (Estimate by word position — insert point ≤ ~50% of the transcript's words. If not, move it earlier or FLAG.)

If any box fails, fix it before showing the user. If rule 4 can't be satisfied, switch to the FLAG case.
