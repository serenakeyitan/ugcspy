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
      text: { type: "mrkdwn", text, verbatim: true },
    },
    {
      type: "context",
      elements: [
        {
          type: "mrkdwn",
          verbatim: true,
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

// Slack mrkdwn escape (&, <, > → entities). This neutralizes the EXPLICIT
// injection forms — links <url|text>, mentions <@U>, channel broadcasts
// <!channel>/<!here> — which all require a literal `<`. It does NOT by itself
// stop bare `@here`/`@everyone` or bare-URL auto-linking; those are killed
// structurally by `verbatim: true` on every mrkdwn text object (the primary
// defense — see the blocks below). Together they're defense-in-depth: verbatim
// disables auto-parsing, escapeMrkdwn keeps the explicit forms rendering as
// clean literal text. Apply to every untrusted string reaching a payload: the
// creator handle, the video hook/caption, the format tag, the brand.
// https://docs.slack.dev/reference/block-kit/composition-objects/text-object/
export function escapeMrkdwn(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// The brand additionally sits inside an inline-code span in the CTA
// (`/ugcspy-rebrand <id> <brand>`), so it must also lose backticks/newlines that
// would break OUT of the code span. Escape first, then strip code-breakers.
function sanitizeBrand(s: string): string {
  return escapeMrkdwn(s).replace(/[`\n\r]/g, "").trim();
}

export function formatAlert(competitor: Competitor, candidate: BreakoutCandidate): string {
  const { video, ratio } = candidate;
  const hookSnippet = video.hook_text ? `\n> ${escapeMrkdwn(video.hook_text.slice(0, 140))}` : "";
  const tag = video.format_tag ? ` · _${escapeMrkdwn(video.format_tag)}_` : "";
  return `🚨 *${escapeMrkdwn(competitor.handle)}* breakout on ${escapeMrkdwn(competitor.platform)} — *${ratio.toFixed(1)}x* baseline${tag}\n${escapeMrkdwn(video.video_url)}${hookSnippet}`;
}

// The absolute view-threshold REMINDER: a tracked video crossed the creator's
// view bar. Unlike the breakout alert, this is a "go remix this now" nudge — it
// leads with the video link and, when the watch named a target brand, the exact
// /ugcspy-rebrand command so the creator can turn the proven video into their
// own script on the spot.
export function formatThresholdReminder(
  competitor: Competitor,
  candidate: BreakoutCandidate,
  remixBrand: string | null,
): string {
  const { video, threshold } = candidate;
  const hookSnippet = video.hook_text ? `\n> ${escapeMrkdwn(video.hook_text.slice(0, 140))}` : "";
  const crossed = Math.round(threshold).toLocaleString();
  const lead = `🔔 *${escapeMrkdwn(competitor.handle)}* video crossed *${crossed}* views — time to remix it.\n${escapeMrkdwn(video.video_url)}${hookSnippet}`;
  const brand = remixBrand ? sanitizeBrand(remixBrand) : "";
  if (brand) {
    // video.id is the local DB id /ugcspy-rebrand resolves; give the ready command.
    return `${lead}\n\n➡️ Remix it for *${brand}*: \`/ugcspy-rebrand ${video.id} ${brand}\``;
  }
  return `${lead}\n\n➡️ Remix it: \`/ugcspy-rebrand ${video.id} [your-brand]\``;
}

export async function postThresholdReminder(
  webhookUrl: string,
  competitor: Competitor,
  candidate: BreakoutCandidate,
  remixBrand: string | null,
): Promise<SlackPostResult> {
  const { video } = candidate;
  const text = formatThresholdReminder(competitor, candidate, remixBrand);
  // The context block interpolates the brand into mrkdwn too — sanitize it here
  // as well, or an untrusted brand name (<!channel>, a link, a backtick) injects
  // a channel-wide ping / phishing link into the footer even though the lead is
  // clean. (Regression: this path had no test; only the `text` lead was covered.)
  const safeBrand = remixBrand ? sanitizeBrand(remixBrand) : "";
  const blocks = [
    { type: "section", text: { type: "mrkdwn", text, verbatim: true } },
    {
      type: "context",
      elements: [
        {
          type: "mrkdwn",
          verbatim: true,
          text: `views: *${video.view_count.toLocaleString()}* · crossed: *${Math.round(candidate.threshold).toLocaleString()}*${safeBrand ? ` · remix → *${safeBrand}*` : ""}`,
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
