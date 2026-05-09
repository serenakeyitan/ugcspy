import Anthropic from "@anthropic-ai/sdk";
import chalk from "chalk";
import ora from "ora";
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { openDb } from "../db/index.ts";
import { effectiveAnthropicKey, loadConfig } from "../lib/config.ts";
import type { VideoRecord } from "../types.ts";

export interface ForkOptions {
  out?: string;
  copy: boolean;
}

const DEFAULT_BRIEFS_DIR = join(homedir(), ".ugcspy", "briefs");

const BRIEF_SYSTEM = `You are a senior creative strategist who briefs UGC creators.
Given a competitor video, write a structured brief a creator can shoot from in 24h.
Sections, in order, with these exact headings:

# Brief: <punchy title>

## Hook variations
3 alternative hooks, each ≤90 chars, written for first-2-second retention. Numbered list.

## Format
The video format from the closed list (GRWM / POV / talking_head / product_demo / unboxing / tutorial / before_after / voiceover_broll / duet_stitch / other) plus a one-line note on why it works for the source.

## Beat sheet
4-6 numbered beats with timing (e.g. "0:00-0:03 Hook", "0:03-0:08 Setup"). Each beat one sentence.

## Suggested b-roll
Bulleted list of 3-5 b-roll ideas.

## CTA
One-line CTA the creator can drop in the last 2 seconds.

Be concrete. No filler.`;

export async function runFork(idOrUrl: string, opts: ForkOptions): Promise<void> {
  const config = loadConfig();
  const apiKey = effectiveAnthropicKey(config);
  if (!apiKey) {
    console.error(chalk.red("Anthropic API key required for fork. Run `ugcspy init`."));
    process.exit(1);
  }

  const db = openDb();
  const video = lookupVideo(db, idOrUrl);
  if (!video) {
    console.error(chalk.red(`No video found for "${idOrUrl}".`));
    console.error(
      chalk.dim("Pass either a numeric id from `ugcspy search --json` or a video URL."),
    );
    process.exit(1);
  }

  const spinner = ora("Generating brief...").start();
  const brief = await generateBrief(video, apiKey);
  spinner.succeed("Brief ready.");

  if (opts.copy) {
    await copyToClipboard(brief);
    console.log(chalk.green("✓ Brief copied to clipboard."));
    return;
  }

  const path = opts.out
    ? resolve(opts.out)
    : join(DEFAULT_BRIEFS_DIR, `brief-${video.platform}-${video.external_id}.md`);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, brief);
  console.log(chalk.green(`✓ Brief written to ${path}`));
}

function lookupVideo(db: ReturnType<typeof openDb>, idOrUrl: string): VideoRecord | null {
  if (/^\d+$/.test(idOrUrl)) {
    const row = db.prepare(`SELECT * FROM videos WHERE id = ?`).get(Number(idOrUrl));
    return (row as VideoRecord) ?? null;
  }
  const row = db.prepare(`SELECT * FROM videos WHERE video_url = ?`).get(idOrUrl);
  return (row as VideoRecord) ?? null;
}

async function generateBrief(video: VideoRecord, apiKey: string): Promise<string> {
  const client = new Anthropic({ apiKey });
  const userText = [
    `Source video URL: ${video.video_url}`,
    `Platform: ${video.platform}`,
    `Caption: ${video.caption}`,
    `Hook (extracted, source=${video.hook_source}): ${video.hook_text}`,
    `Format tag: ${video.format_tag ?? "unclassified"}`,
    `Stats: ${video.view_count.toLocaleString()} views / ${video.like_count.toLocaleString()} likes`,
    "",
    "Write the brief now.",
  ].join("\n");

  const response = await client.messages.create({
    model: "claude-sonnet-4-6",
    max_tokens: 1500,
    system: BRIEF_SYSTEM,
    messages: [{ role: "user", content: userText }],
  });
  const block = response.content[0];
  if (!block || block.type !== "text") return "(empty response)";
  return block.text;
}

async function copyToClipboard(text: string): Promise<void> {
  const platform = process.platform;
  const cmd =
    platform === "darwin"
      ? ["pbcopy"]
      : platform === "win32"
        ? ["clip"]
        : ["xclip", "-selection", "clipboard"];
  const proc = Bun.spawn(cmd, { stdin: "pipe" });
  proc.stdin.write(text);
  await proc.stdin.end();
  await proc.exited;
}
