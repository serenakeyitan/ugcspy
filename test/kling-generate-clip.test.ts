import { describe, expect, test } from "bun:test";
import { KlingProvider } from "../src/render/kling.ts";

/**
 * Guard the defense-in-depth duration check added in PR #13. Kling text2video
 * only renders 5s or 10s segments; if a caller forgets to round, the adapter
 * should refuse loudly instead of silently truncating a 14s render to 10s.
 *
 * The matching invariant lives in
 * `vendor/video-recipe/scripts/compose.py:kling_billed_duration`. If you
 * change one, change the other and update both tests.
 */
describe("KlingProvider.generateClip duration guard", () => {
  test("rejects duration_sec > 10 with a clear error", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "x", duration_sec: 14 }),
    ).rejects.toThrow(/duration_sec=14/);
  });

  test("rejects duration_sec >> 10 with a clear error", async () => {
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "x", duration_sec: 47 }),
    ).rejects.toThrow(/Kling std supports 5s or 10s/);
  });

  test("rejects when caller bypasses kling_billed_duration but provides an in-range value", async () => {
    // Boundary case: 10.0001s passes the < int rounding in some langs
    // but fails the > 10 guard. Confirms the strict-greater-than.
    const provider = new KlingProvider("access", "secret");
    await expect(
      provider.generateClip({ prompt: "x", duration_sec: 10.0001 }),
    ).rejects.toThrow(/duration_sec=10.0001/);
  });

  test("rejects prompts longer than Kling's 2500-char cap (issue #30)", async () => {
    // Codex flagged: compose.py's L1 transcript injection truncates the
    // appended portion but doesn't cap total prompt length. A long
    // base_prompt + 300-char append could exceed Kling's text2video
    // cap and fail at submit with a cryptic API error. The guard here
    // catches it upfront with a clear remediation pointing at recipe.json.
    const provider = new KlingProvider("access", "secret");
    const longPrompt = "x".repeat(2501);
    await expect(
      provider.generateClip({ prompt: longPrompt, duration_sec: 5 }),
    ).rejects.toThrow(/2500/);
  });

  // Note: we don't test "accepts prompts at exactly 2500 chars" because
  // the happy path requires mocking the full Kling submit+poll+download
  // cycle, which is covered in test/kling-lipsync.test.ts. Asserting
  // the inverse ("not.toThrow(/2500/)") would create a flaky test
  // that depends on what error fires NEXT (network, JWT, etc.) when
  // we lack mocks. The boundary correctness is locked by the
  // implementation's `> PROMPT_CHAR_LIMIT` comparison + the 2501-char
  // rejection test above.

  // Note: we don't test the happy path here because that requires
  // mocking the full Kling submit+poll+download cycle, which is
  // covered in test/kling-lipsync.test.ts via makeMockFetch. The
  // sole purpose of this file is the upfront duration + prompt guards.
});
