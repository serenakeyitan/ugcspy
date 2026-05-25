import { existsSync } from "node:fs";
import { homedir, platform } from "node:os";
import { join } from "node:path";

export const VENV_DIR = join(homedir(), ".ugcspy", "venv");

export function venvPython(): string {
  const binDir = platform() === "win32" ? "Scripts" : "bin";
  const exe = platform() === "win32" ? "python.exe" : "python";
  return join(VENV_DIR, binDir, exe);
}

export function venvExists(): boolean {
  return existsSync(venvPython());
}
