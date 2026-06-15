// Instagram view-count enrichment depth.
//
// Background: the IG bridge walks a creator's full roster cheaply (gallery-dl →
// likes/caption/url for every post), then enriches the top posts with view/play
// counts via instaloader's per-post GraphQL call. That enrich step costs ~4s per
// post (the GraphQL latency, NOT a rate limit — measured: 40 posts back-to-back,
// zero throttle, ~13 posts/min). So "more views" = "proportionally longer wait",
// and the user trades depth for speed.
//
// Tiers are the user-facing choice; each maps to a post count + a human time
// estimate at the measured ~13/min.

export type IgEnrichTier = "quick" | "standard" | "deep";

export interface IgEnrichSpec {
  tier: IgEnrichTier;
  count: number;
  label: string;
  estimate: string;
}

// ~13 posts/min measured (≈4.6s/post incl. a small safety sleep). Estimates are
// rounded to a friendly number.
export const IG_ENRICH_TIERS: Record<IgEnrichTier, IgEnrichSpec> = {
  quick: { tier: "quick", count: 15, label: "Quick", estimate: "~70s" },
  standard: { tier: "standard", count: 40, label: "Standard", estimate: "~3 min" },
  deep: { tier: "deep", count: 100, label: "Deep", estimate: "~8 min" },
};

export const DEFAULT_IG_ENRICH_TIER: IgEnrichTier = "standard";

// Below this, no point prompting — the wait is trivial, just enrich.
export const IG_ENRICH_PROMPT_THRESHOLD = IG_ENRICH_TIERS.quick.count;

export function isIgEnrichTier(v: unknown): v is IgEnrichTier {
  return v === "quick" || v === "standard" || v === "deep";
}

// Resolve a CLI/config value to a concrete post count. Accepts a tier name OR a
// raw integer (power users can pass --enrich 250). Falls back to the default
// tier on anything unrecognized.
export function resolveEnrichCount(value: string | number | undefined): number {
  if (value === undefined || value === null || value === "") {
    return IG_ENRICH_TIERS[DEFAULT_IG_ENRICH_TIER].count;
  }
  if (isIgEnrichTier(value)) return IG_ENRICH_TIERS[value].count;
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isInteger(n) && n > 0) return n;
  return IG_ENRICH_TIERS[DEFAULT_IG_ENRICH_TIER].count;
}

// Friendly time estimate for an arbitrary post count, for the "raw N" path.
export function enrichEstimate(count: number): string {
  const mins = count / 13;
  if (mins < 1.2) return `~${Math.max(20, Math.round(count * 4.6))}s`;
  return `~${Math.round(mins)} min`;
}
