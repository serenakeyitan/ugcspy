import { writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createHmac } from "node:crypto";
import { RenderError, type ClipGenRequest, type ClipGenResult, type VideoGenProvider } from "./types.ts";

/**
 * Kling (kling.ai / Kuaishou) text-to-video. $0.10/sec for std mode,
 * cheapest commercial-use video-gen API as of May 2026.
 *
 * Auth (verified empirically against api.klingai.com on a real account):
 *   - Two credentials per account: access_key + secret_key
 *   - Each request signs a fresh HS256 JWT:
 *       header  = {alg: HS256, typ: JWT}
 *       payload = {iss: <access_key>, exp: now+30min, nbf: now-5s}
 *       sig     = HMAC-SHA256(secret_key, base64url(header)+"."+base64url(payload))
 *     Send as `Authorization: Bearer <jwt>`
 *
 * Wire format: confirmed against https://api.klingai.com/v1/videos/text2video
 * on 2026-05-24 — GET to list jobs returns {code:0, message:"SUCCEED"} when
 * auth is good, distinct from a 401 on bad auth.
 *
 * Polling: submit → returns task_id immediately → poll
 * /v1/videos/text2video/<task_id> every 5s until task_status="succeed"
 * (typical 1-3 min for std mode).
 */
export class KlingProvider implements VideoGenProvider {
  readonly name = "kling";
  readonly cost_per_second_usd = 0.10;

  // Base URL is api.klingai.com (NOT api.kling.ai — that domain doesn't host
  // the dev API). Verified empirically.
  private readonly base = "https://api.klingai.com";

  constructor(private accessKey: string, private secretKey: string) {}

  async generateClip(req: ClipGenRequest): Promise<ClipGenResult> {
    this.assertConfigured();
    // Kling std supports 5s or 10s segments; round up to nearest supported.
    const duration = req.duration_sec <= 5 ? 5 : 10;
    const aspect = req.aspect_ratio ?? "9:16";

    // 1. Submit job
    const submitRes = await this.fetchSigned("/v1/videos/text2video", {
      method: "POST",
      body: JSON.stringify({
        model_name: "kling-v1-6",
        prompt: req.prompt,
        duration: String(duration),
        aspect_ratio: aspect,
        mode: "std", // "pro" is ~2x cost for marginal quality
      }),
    });
    if (!submitRes.ok) {
      throw new RenderError(
        `Kling submit failed: ${submitRes.status} ${await submitRes.text()}`,
        this.name,
      );
    }
    const submitJson = (await submitRes.json()) as {
      code?: number;
      message?: string;
      data?: { task_id?: string };
    };
    if (submitJson.code !== 0) {
      throw new RenderError(
        `Kling submit returned code=${submitJson.code} message="${submitJson.message}"`,
        this.name,
      );
    }
    const taskId = submitJson.data?.task_id;
    if (!taskId) throw new RenderError("Kling response missing data.task_id", this.name);

    // 2. Poll until done
    const startedAt = Date.now();
    const POLL_INTERVAL_MS = 5000;
    const TIMEOUT_MS = 8 * 60 * 1000;
    let videoUrl: string | null = null;
    while (Date.now() - startedAt < TIMEOUT_MS) {
      await sleep(POLL_INTERVAL_MS);
      const statusRes = await this.fetchSigned(`/v1/videos/text2video/${taskId}`);
      if (!statusRes.ok) continue; // transient — retry on next tick
      const statusJson = (await statusRes.json()) as {
        data?: {
          task_status?: string;
          task_status_msg?: string;
          task_result?: { videos?: { url?: string }[] };
        };
      };
      const status = statusJson.data?.task_status;
      if (status === "succeed") {
        videoUrl = statusJson.data?.task_result?.videos?.[0]?.url ?? null;
        break;
      }
      if (status === "failed") {
        throw new RenderError(
          `Kling job ${taskId} failed: ${statusJson.data?.task_status_msg ?? "no message"}`,
          this.name,
        );
      }
      // "submitted" or "processing" — keep polling
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

  /** Fail fast with an actionable message if keys are unconfigured. */
  private assertConfigured(): void {
    if (!this.accessKey || !this.secretKey) {
      throw new RenderError(
        "Kling credentials missing. Set both KLING_ACCESS_KEY and KLING_SECRET_KEY env vars (get them from https://klingai.com/dev — needs the dev account, not just the web app).",
        this.name,
      );
    }
  }

  /** Sign a fresh JWT for each request — Kling's per-request expiry is 30min. */
  private mintToken(): string {
    const now = Math.floor(Date.now() / 1000);
    const header = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = b64url(JSON.stringify({ iss: this.accessKey, exp: now + 1800, nbf: now - 5 }));
    const signingInput = `${header}.${payload}`;
    const sig = createHmac("sha256", this.secretKey).update(signingInput).digest();
    return `${signingInput}.${b64urlBuf(sig)}`;
  }

  private async fetchSigned(path: string, init: RequestInit = {}): Promise<Response> {
    const token = this.mintToken();
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(`${this.base}${path}`, { ...init, headers });
  }
}

function b64url(s: string): string {
  return b64urlBuf(Buffer.from(s, "utf8"));
}

function b64urlBuf(b: Buffer): string {
  return b.toString("base64").replace(/=+$/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
