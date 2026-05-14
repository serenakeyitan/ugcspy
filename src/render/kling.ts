import { writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { RenderError, type ClipGenRequest, type ClipGenResult, type VideoGenProvider } from "./types.ts";

/**
 * Kling 3.0 (kling.ai / Kuaishou). $0.10/sec, currently the cheapest
 * commercial-use video-gen API. Polling-based: submit a job, get a task_id,
 * poll until status=complete, download the result.
 *
 * Wire format docs: https://docs.kling.ai/api (verify before first run —
 * the API has been evolving fast through 2025-26).
 *
 * This is a stub implementation that produces a clear error if no API
 * key is configured, so the pipeline degrades gracefully and the user
 * sees actionable guidance instead of a stack trace.
 */
export class KlingProvider implements VideoGenProvider {
  readonly name = "kling";
  readonly cost_per_second_usd = 0.10;

  constructor(private apiKey: string) {}

  async generateClip(req: ClipGenRequest): Promise<ClipGenResult> {
    if (!this.apiKey) {
      throw new RenderError(
        "Kling API key missing. Run `ugcspy init --yes --kling-api-key <key>` or set KLING_API_KEY env var.",
        this.name,
      );
    }
    // Kling 3.0 supports 5s or 10s segments; round up to nearest supported.
    const duration = req.duration_sec <= 5 ? 5 : 10;
    const aspect = req.aspect_ratio ?? "9:16";

    // 1. Submit job
    const submitRes = await fetch("https://api.kling.ai/v1/videos/text2video", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${this.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model_name: "kling-v1-6",
        prompt: req.prompt,
        duration: String(duration),
        aspect_ratio: aspect,
        mode: "std", // "pro" is 2x cost for marginal quality bump
      }),
    });
    if (!submitRes.ok) {
      throw new RenderError(
        `Kling submit failed: ${submitRes.status} ${await submitRes.text()}`,
        this.name,
      );
    }
    const submitJson = (await submitRes.json()) as { data?: { task_id?: string } };
    const taskId = submitJson.data?.task_id;
    if (!taskId) throw new RenderError("Kling response missing task_id", this.name);

    // 2. Poll until done (Kling typically takes 1-3 min for std mode)
    const startedAt = Date.now();
    const POLL_INTERVAL_MS = 5000;
    const TIMEOUT_MS = 8 * 60 * 1000;
    let videoUrl: string | null = null;
    while (Date.now() - startedAt < TIMEOUT_MS) {
      await sleep(POLL_INTERVAL_MS);
      const statusRes = await fetch(
        `https://api.kling.ai/v1/videos/text2video/${taskId}`,
        { headers: { "Authorization": `Bearer ${this.apiKey}` } },
      );
      if (!statusRes.ok) continue;
      const statusJson = (await statusRes.json()) as {
        data?: { task_status?: string; task_result?: { videos?: { url?: string }[] } };
      };
      const status = statusJson.data?.task_status;
      if (status === "succeed") {
        videoUrl = statusJson.data?.task_result?.videos?.[0]?.url ?? null;
        break;
      }
      if (status === "failed") {
        throw new RenderError(`Kling job ${taskId} failed`, this.name);
      }
    }
    if (!videoUrl) {
      throw new RenderError(`Kling job ${taskId} timed out after 8min`, this.name);
    }

    // 3. Download to local temp
    const outDir = join(tmpdir(), "ugcspy-renders");
    await mkdir(outDir, { recursive: true });
    const outPath = join(outDir, `kling-${taskId}.mp4`);
    const dl = await fetch(videoUrl);
    if (!dl.ok) {
      throw new RenderError(`Kling download failed: ${dl.status}`, this.name);
    }
    const buf = Buffer.from(await dl.arrayBuffer());
    writeFileSync(outPath, buf);

    return {
      mp4_path: outPath,
      external_id: taskId,
      cost_usd: duration * this.cost_per_second_usd,
    };
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
