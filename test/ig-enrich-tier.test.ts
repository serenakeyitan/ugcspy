import { describe, expect, test } from "bun:test";
import {
  DEFAULT_IG_ENRICH_TIER,
  IG_ENRICH_TIERS,
  enrichEstimate,
  isIgEnrichTier,
  resolveEnrichCount,
} from "../src/lib/ig-enrich-tier.ts";
import { resolveIgEnrichCount } from "../src/commands/search.ts";

describe("IG enrich tiers", () => {
  test("three tiers, ascending counts", () => {
    expect(IG_ENRICH_TIERS.quick.count).toBeLessThan(IG_ENRICH_TIERS.standard.count);
    expect(IG_ENRICH_TIERS.standard.count).toBeLessThan(IG_ENRICH_TIERS.deep.count);
  });

  test("default tier is standard", () => {
    expect(DEFAULT_IG_ENRICH_TIER).toBe("standard");
  });

  test("isIgEnrichTier accepts only the three names", () => {
    expect(isIgEnrichTier("quick")).toBe(true);
    expect(isIgEnrichTier("deep")).toBe(true);
    expect(isIgEnrichTier("turbo")).toBe(false);
    expect(isIgEnrichTier(40)).toBe(false);
  });
});

describe("resolveEnrichCount", () => {
  test("tier name → that tier's count", () => {
    expect(resolveEnrichCount("quick")).toBe(IG_ENRICH_TIERS.quick.count);
    expect(resolveEnrichCount("deep")).toBe(IG_ENRICH_TIERS.deep.count);
  });

  test("a raw positive integer (string or number) passes through", () => {
    expect(resolveEnrichCount("250")).toBe(250);
    expect(resolveEnrichCount(7)).toBe(7);
  });

  test("undefined / empty / garbage → default tier count", () => {
    const def = IG_ENRICH_TIERS[DEFAULT_IG_ENRICH_TIER].count;
    expect(resolveEnrichCount(undefined)).toBe(def);
    expect(resolveEnrichCount("")).toBe(def);
    expect(resolveEnrichCount("banana")).toBe(def);
    expect(resolveEnrichCount(-5)).toBe(def);
    expect(resolveEnrichCount(0)).toBe(def);
  });
});

describe("enrichEstimate", () => {
  test("small counts read in seconds, big counts in minutes", () => {
    expect(enrichEstimate(15)).toMatch(/s$/);
    expect(enrichEstimate(100)).toMatch(/min$/);
  });
});

describe("resolveIgEnrichCount precedence (the chooser)", () => {
  test("explicit --enrich tier wins, never prompts (even if interactive)", async () => {
    const n = await resolveIgEnrichCount({ enrich: "deep", json: false }, /*interactive*/ true);
    expect(n).toBe(IG_ENRICH_TIERS.deep.count);
  });

  test("explicit --enrich raw count wins", async () => {
    const n = await resolveIgEnrichCount({ enrich: "250", json: false }, true);
    expect(n).toBe(250);
  });

  test("no --enrich + non-interactive → default tier, NO prompt", async () => {
    const n = await resolveIgEnrichCount({ enrich: undefined, json: true }, /*interactive*/ false);
    expect(n).toBe(IG_ENRICH_TIERS[DEFAULT_IG_ENRICH_TIER].count);
  });
});
