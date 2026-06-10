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
