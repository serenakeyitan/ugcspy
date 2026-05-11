# Identify the hook

You are running the `video-recipe` skill. After stage 4 (per-cut classification), you have:

- `recipes/<id>/cuts.json` — every cut with start/end times
- `recipes/<id>/cuts/<i>/inferred.json` — your per-cut classifications and inferred prompts
- `recipes/<id>/cuts/<i>/transcript.json` (optional) — voiceover words paired to each cut
- `recipes/<id>/cuts/<i>/{a,b,c}.jpg` — keyframes for each cut

This file tells you how to identify the **hook** — the opening 1-3 seconds of the video that earns the viewer's attention.

The hook is a property of the **video**, not of any single cut. It usually lives in cut 0 alone, sometimes spans cuts 0+1.

## Step 1: Look at the first 3 seconds

Read the keyframes and transcripts for every cut whose `start_sec` is less than 3.0. Often that's just cut 0; sometimes 0 and 1. If cut 0 is already >2 seconds long, the hook is just cut 0.

## Step 2: Pick the pattern

Pick exactly one of these 5 patterns, or `null` if no clear hook exists:

| Pattern | What it looks like |
|---|---|
| `question` | The opening transcript or caption is a direct question to the viewer. "What if I told you...", "Did you know...", "Have you ever wondered..." |
| `claim` | A definitive statement, often with a specific number or unexpected fact. "I made $1M with AI in 30 days." "This took 4 hours." "Nobody talks about this." |
| `shock_cut` | Opens mid-action with a visually surprising or attention-grabbing image. The visual carries the hook regardless of what's said. |
| `transformation_tease` | Opens with the END state, promising payoff. "Here's the result..." then cut to before-state. Often "After this..." or shows the finished version first. |
| `pattern_break` | Visually or audibly violates the genre's expected opening. A sketch comedy that opens like a documentary. A vlog that opens with cinematic framing. The dissonance is the hook. |
| `null` | Video meanders into its content with no clear hook structure. Long preamble, no question, no claim, no shock. |

When in doubt between two patterns, pick the one supported by the transcript+caption. If the visual says one thing and the words say another, the **words decide the pattern** (the hook is what makes someone keep watching, which is usually the spoken/written claim).

## Step 3: Write `recipes/<id>/hook.json`

If you found a hook, write a single JSON object via the Write tool:

```json
{
  "duration_sec": 2.4,
  "spans_cuts": [0, 1],
  "pattern": "shock_cut",
  "text": "<the on-screen caption or title text in the first 3s, or null>",
  "voiceover": "<the spoken words in the first 3s, or null>",
  "first_visual": "<the inferred prompt for cut 0, or null if cut 0 isn't an ai_clip>"
}
```

If the video has no clear hook, write:

```json
{"hook": null}
```

`duration_sec` is the duration of the cuts the hook spans. `spans_cuts` is a list of cut indices.

## Examples

**PJ Ace × Greg Isenberg interview** — cut 0 is Greg on camera saying "What if I can show you the exact workflow for how to come up with AI...", with an "Exact Workflow" caption overlay:

```json
{
  "duration_sec": 4.0,
  "spans_cuts": [0],
  "pattern": "question",
  "text": "Exact Workflow / for how to come up",
  "voiceover": "What if I can show you the exact workflow for how to come up with AI",
  "first_visual": null
}
```

**Sora announcement** — cut 0 is a 11s static title card with a research disclaimer:

```json
{"hook": null}
```

(The disclaimer paragraph is informational, not a hook.)

**Ghibli LOTR** — cut 0 is a 3.8s Ghibli-painted misty-mountain title plate with "STUDIO GHIBLI PRESENTS / The Fellowship of the Ring":

```json
{
  "duration_sec": 3.83,
  "spans_cuts": [0],
  "pattern": "pattern_break",
  "text": "STUDIO GHIBLI PRESENTS — The Fellowship of the Ring",
  "voiceover": null,
  "first_visual": "Studio-Ghibli-style hand-painted establishing shot of a sweeping fjord vista at dawn..."
}
```

(The hook is the unexpected pairing — Ghibli aesthetics on a Tolkien story.)

## What NOT to do

- Don't pick a pattern just because the video opens with content. Plenty of videos have no hook; `null` is a valid output.
- Don't include the second cut in `spans_cuts` unless it materially extends the hook. If cut 0 already establishes the hook in 4 seconds, don't add cut 1 just because it falls under the 3-second window.
- Don't paraphrase the text or voiceover — quote it as it appeared.
- Don't write prose around the JSON. Just the object.
