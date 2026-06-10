import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { InvalidArgumentError } from "commander";
import { positiveFloat, positiveInt } from "../src/lib/cli-args.ts";

// ── pure validators ──────────────────────────────────────────────────────────

describe("positiveInt", () => {
  test("accepts plain positive integers", () => {
    expect(positiveInt("20")).toBe(20);
    expect(positiveInt(" 7 ")).toBe(7);
  });

  test("rejects the NaN/partial-parse family that bare parseInt let through", () => {
    expect(() => positiveInt("abc")).toThrow(InvalidArgumentError); // was NaN → zero rows
    expect(() => positiveInt("6h")).toThrow(InvalidArgumentError); // was 6 → 6ms hot loop
    expect(() => positiveInt("1.5")).toThrow(InvalidArgumentError);
    expect(() => positiveInt("0")).toThrow(InvalidArgumentError);
    expect(() => positiveInt("-3")).toThrow(InvalidArgumentError);
    expect(() => positiveInt("")).toThrow(InvalidArgumentError);
  });
});

describe("positiveFloat", () => {
  test("accepts positive numbers", () => {
    expect(positiveFloat("2.5")).toBe(2.5);
    expect(positiveFloat("2")).toBe(2);
    expect(positiveFloat(".5")).toBe(0.5);
  });

  test("rejects NaN/garbage (was: NaN → NULL threshold → alert spam)", () => {
    expect(() => positiveFloat("abc")).toThrow(InvalidArgumentError);
    expect(() => positiveFloat("2x")).toThrow(InvalidArgumentError);
    expect(() => positiveFloat("0")).toThrow(InvalidArgumentError);
    expect(() => positiveFloat("-1.5")).toThrow(InvalidArgumentError);
    expect(() => positiveFloat("")).toThrow(InvalidArgumentError);
  });
});

// ── the real CLI wiring ──────────────────────────────────────────────────────
// These spawn src/cli.ts but ONLY exercise paths where commander rejects (or
// answers --version) BEFORE any action runs — so they never touch ~/.ugcspy.

const CLI = join(import.meta.dir, "..", "src", "cli.ts");

async function runCli(args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const proc = Bun.spawn(["bun", CLI, ...args], { stdout: "pipe", stderr: "pipe" });
  const [stdout, stderr] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ]);
  const exitCode = await proc.exited;
  return { exitCode, stdout, stderr };
}

describe("cli.ts wiring (spawned, action never runs)", () => {
  test("--version is single-sourced from package.json", async () => {
    const pkg = (await Bun.file(join(import.meta.dir, "..", "package.json")).json()) as {
      version: string;
    };
    const r = await runCli(["--version"]);
    expect(r.exitCode).toBe(0);
    expect(r.stdout.trim()).toBe(pkg.version);
  });

  test("search --limit abc exits non-zero with a clear message (was: silent zero rows)", async () => {
    const r = await runCli(["search", "befreed", "--limit", "abc"]);
    expect(r.exitCode).not.toBe(0);
    expect(r.stderr).toContain("positive integer");
  });

  test("daemon --interval 6h exits non-zero (was: parsed as a 6ms hot loop)", async () => {
    const r = await runCli(["daemon", "--interval", "6h"]);
    expect(r.exitCode).not.toBe(0);
    expect(r.stderr).toContain("positive integer");
  });

  test("watch add --threshold abc exits non-zero (was: NaN → NULL → alert spam)", async () => {
    const r = await runCli(["watch", "add", "@x", "--threshold", "abc"]);
    expect(r.exitCode).not.toBe(0);
    expect(r.stderr).toContain("positive number");
  });

  test("init --provider bogus exits non-zero listing the valid providers (validates before any write)", async () => {
    const r = await runCli(["init", "--yes", "--provider", "bogus"]);
    expect(r.exitCode).not.toBe(0);
    expect(r.stderr).toContain("tiktok-oss, scrapecreators, mock");
  });
});
