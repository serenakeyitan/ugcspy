# Kling API notes

Hard-won gotchas from running the official Kling API (`api-singapore.klingai.com`)
for real. These are the things the docs don't make obvious and that only
surface on a live paid call — not in a dry-run. All values below were verified
against the live endpoint (May–June 2026), not copied from a third-party
mirror. The integration lives in `src/render/kling.ts` (TypeScript adapter);
`scripts/compose.py` drives it over the stdin/stdout `ugcspy render` boundary.

## 1. Lip-sync `video_id` is the VIDEO id, not the task id

When a `text2video` / `image2video` task succeeds, the result is:

```jsonc
{ "data": {
    "task_id": "890285348722049090",          // the submit→poll job id
    "task_result": { "videos": [
      { "id": "890285348797554740",            // the GENERATED VIDEO id  ← different!
        "url": "https://.../output.mp4",
        "duration": "5" } ] } } }
```

The two IDs are **different values**. Lip-sync (`/v1/videos/lip-sync`,
`input.video_id`) looks the argument up as a *video object*, so it must be
`task_result.videos[0].id`. Passing the `task_id` yields:

```
400 {"code":1201,"message":"From video not found by id: 890285348722049090"}
```

`generateClip` therefore returns BOTH: `external_id` = the task id (traceability),
and `video_id` = the generated video's id (what lip-sync needs). `compose.py`
uses `video_id` and falls back to `external_id` only for providers that don't
distinguish the two. See `ClipGenResult.video_id` in `src/render/types.ts`.

## 2. Lip-sync text2video REQUIRES `voice_id` — language alone is not enough

For `mode: "text2video"`, both `voice_language` AND `voice_id` are required.
There is **no implicit default** — Kling does not pick a voice from the
language. Sending only `voice_language: "en"` yields:

```
400 {"code":1201,"message":"Voice language not found"}
```

(Note the misleading message — it says "language not found" but the real
cause is the missing `voice_id`.)

The adapter supplies a known-valid default per language when the caller
doesn't specify one (`KlingProvider.DEFAULT_VOICE_ID`):

| Language | Default voice id        | Note            |
| -------- | ----------------------- | --------------- |
| `en`     | `girlfriend_4_speech02` | natural female  |
| `zh`     | `ai_shatang`            | female          |

**The catalog is account/endpoint-specific.** Voice IDs documented for fal.ai
or PiAPI are NOT necessarily valid on the official `api-singapore` endpoint.
Verified-valid `en` IDs (probed live): `girlfriend_4_speech02` (F),
`chat1_female_new-3` (F), `genshin_vindi2`, `ai_kaiya`, `uk_boy1`,
`oversea_male1`. Verified-INVALID here despite appearing in fal/PiAPI lists:
`oversea_female1`, `commercial_lady_en_f-v1`, `reader_en_m-v1`. There is no
public unauthenticated voice-list endpoint (`/v1/.../voices` 404s;
`klingai.com/api/lip/sync/ttsList` needs a logged-in session). Probe with a
short dummy text before baking a new id into the default map.

## 3. Trial packs bill in UNITS and cap concurrency

A trial resource pack (`Trial-Video-100Units-5Con-1Months`) bills in **units,
not USD** — `compose.py`'s `$`-cost is a local estimate mirror, not the actual
deduction. Check real consumption via `GET /account/costs?start_time=&end_time=`
(ms epoch): the response's `resource_pack_subscribe_infos[].remaining_quantity`
is authoritative.

The `5Con` in the pack name is a **5-concurrent-task cap**. Exceeding it returns:

```
parallel task over resource pack limit
```

This matters when probing voice ids in a loop or rendering many cuts at once —
throttle to ≤5 in-flight tasks.

## Auth recap

HMAC-HS256 JWT, `iss` = access key, `exp` = now+1800s, `nbf` = now−5s, sent as
`Authorization: Bearer <jwt>`. Keys come from env (`KLING_ACCESS_KEY` /
`KLING_SECRET_KEY`) via `src/commands/render.ts`; `KLING_API_KEY` is accepted as
a legacy alias for the access key only. Base URL defaults to
`api-singapore.klingai.com` (non-China); override with `KLING_BASE_URL`.
