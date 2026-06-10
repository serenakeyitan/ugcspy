import { describe, expect, test } from "bun:test";
import { existsSync, statSync } from "node:fs";
import { platform } from "node:os";
import { join } from "node:path";
import { resolveAudioRequirements, resolveRequirements } from "../src/commands/install-deps.ts";
import { venvPython } from "../src/lib/venv.ts";

describe("install-deps resource resolution (repo-layout contract)", () => {
  // runInstallDeps is the onboarding-critical path. A scripts/ rename or move
  // used to surface only on a user's machine as "Requirements file missing" —
  // this pins the layout in CI instead.
  test("requirements.txt resolves to an existing, non-empty file", () => {
    const p = resolveRequirements();
    expect(existsSync(p)).toBe(true);
    expect(statSync(p).size).toBeGreaterThan(0);
  });

  test("requirements-audio.txt resolves to an existing, non-empty file", () => {
    const p = resolveAudioRequirements();
    expect(existsSync(p)).toBe(true);
    expect(statSync(p).size).toBeGreaterThan(0);
  });
});

describe("venvPython platform layout", () => {
  test("points at the managed venv's interpreter for this platform", () => {
    const expected =
      platform() === "win32"
        ? join("venv", "Scripts", "python.exe")
        : join("venv", "bin", "python");
    expect(venvPython().endsWith(expected)).toBe(true);
  });
});
