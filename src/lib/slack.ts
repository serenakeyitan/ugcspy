import type { BreakoutCandidate } from "./breakout.ts";
import type { Competitor } from "../types.ts";

export interface SlackPostResult {
  ok: boolean;
  status: number;
  body: string;
}

export async function postBreakoutAlert(
  webhookUrl: string,
  competitor: Competitor,
  candidate: BreakoutCandidate,
): Promise<SlackPostResult> {
  const { video, ratio, threshold } = candidate;
  const text = formatAlert(competitor, candidate);
  const blocks = [
    {
      type: "section",
      text: { type: "mrkdwn", text },
    },
    {
      type: "context",
      elements: [
        {
          type: "mrkdwn",
          text: `views: *${video.view_count.toLocaleString()}* · ratio: *${ratio.toFixed(2)}x* · threshold: *${Math.round(threshold).toLocaleString()}*`,
        },
      ],
    },
  ];
  // 10s cap: a stalled webhook must not wedge a daemon tick. Network errors
  // and timeouts come back as a failed result (status 0) instead of a throw,
  // so one watch's bad webhook can't abort the alerts/watches after it.
  try {
    const res = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, blocks }),
      signal: AbortSignal.timeout(10_000),
    });
    return { ok: res.ok, status: res.status, body: await res.text() };
  } catch (err) {
    return { ok: false, status: 0, body: (err as Error).message };
  }
}

export function formatAlert(competitor: Competitor, candidate: BreakoutCandidate): string {
  const { video, ratio } = candidate;
  const hookSnippet = video.hook_text ? `\n> ${video.hook_text.slice(0, 140)}` : "";
  const tag = video.format_tag ? ` · _${video.format_tag}_` : "";
  return `🚨 *${competitor.handle}* breakout on ${competitor.platform} — *${ratio.toFixed(1)}x* baseline${tag}\n${video.video_url}${hookSnippet}`;
}

// The absolute view-threshold REMINDER: a tracked video crossed the creator's
// view bar. Unlike the breakout alert, this is a "go remix this now" nudge — it
// leads with the video link and, when the watch named a target brand, the exact
// /ugcspy-rebrand command so the creator can turn the proven video into their
// own script on the spot.
// Neutralize Slack mrkdwn control chars in untrusted text (the user-supplied
// remix brand). Strips `<>` (which create links/<!channel> mentions), backticks
// (which would break out of the inline-code CTA), and newlines/asterisks — a
// brand name needs none of these, so removing them is lossless in practice and
// closes the injection/format-break that codex flagged.
function sanitizeBrand(s: string): string {
  return s.replace(/[<>`*\n\r]/g, "").trim();
}

export function formatThresholdReminder(
  competitor: Competitor,
  candidate: BreakoutCandidate,
  remixBrand: string | null,
): string {
  const { video, threshold } = candidate;
  const hookSnippet = video.hook_text ? `\n> ${video.hook_text.slice(0, 140)}` : "";
  const crossed = Math.round(threshold).toLocaleString();
  const lead = `🔔 *${competitor.handle}* video crossed *${crossed}* views — time to remix it.\n${video.video_url}${hookSnippet}`;
  const brand = remixBrand ? sanitizeBrand(remixBrand) : "";
  if (brand) {
    // video.id is the local DB id /ugcspy-rebrand resolves; give the ready command.
    return `${lead}\n\n➡️ Remix it for *${brand}*: \`/ugcspy-rebrand ${video.id} ${brand}\``;
  }
  return `${lead}\n\n➡️ Remix it: \`/ugcspy-rebrand ${video.id} <your-brand>\``;
}

export async function postThresholdReminder(
  webhookUrl: string,
  competitor: Competitor,
  candidate: BreakoutCandidate,
  remixBrand: string | null,
): Promise<SlackPostResult> {
  const { video } = candidate;
  const text = formatThresholdReminder(competitor, candidate, remixBrand);
  const blocks = [
    { type: "section", text: { type: "mrkdwn", text } },
    {
      type: "context",
      elements: [
        {
          type: "mrkdwn",
          text: `views: *${video.view_count.toLocaleString()}* · crossed: *${Math.round(candidate.threshold).toLocaleString()}*${remixBrand ? ` · remix → *${remixBrand}*` : ""}`,
        },
      ],
    },
  ];
  try {
    const res = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, blocks }),
      signal: AbortSignal.timeout(10_000),
    });
    return { ok: res.ok, status: res.status, body: await res.text() };
  } catch (err) {
    return { ok: false, status: 0, body: (err as Error).message };
  }
}
