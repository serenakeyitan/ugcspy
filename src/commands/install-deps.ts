import chalk from "chalk";
import ora from "ora";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { VENV_DIR, venvPython } from "../lib/venv.ts";

const PYTHON_CANDIDATES = ["python3", "python"] as const;

interface RunResult {
  ok: boolean;
  stdout: string;
  stderr: string;
}

export async function runInstallDeps(): Promise<void> {
  console.log(chalk.bold("\nInstalling tiktok-oss provider dependencies...\n"));

  const systemPython = await findPython();
  if (!systemPython) {
    console.error(chalk.red("✖ Python 3 not found on PATH."));
    console.error(`  Install Python 3.9+ first: ${chalk.cyan("https://www.python.org/downloads/")}`);
    process.exit(1);
  }
  console.log(chalk.dim(`Using ${systemPython} to bootstrap a managed venv at ${VENV_DIR}.\n`));

  const requirementsPath = resolveRequirements();
  if (!existsSync(requirementsPath)) {
    console.error(chalk.red(`✖ Requirements file missing: ${requirementsPath}`));
    process.exit(1);
  }

  // Step 1: create (or reuse) the venv. `python -m venv` is idempotent —
  // re-running on an existing venv is fine and fast.
  const venv = ora(`Creating venv at ${VENV_DIR}`).start();
  const venvResult = await run(systemPython, ["-m", "venv", VENV_DIR]);
  if (!venvResult.ok) {
    venv.fail("venv creation failed");
    console.error(chalk.dim(venvResult.stderr.slice(-2000)));
    console.error(
      chalk.dim(
        "\nOn Debian/Ubuntu this may need `apt install python3-venv`. On macOS this should work out of the box.",
      ),
    );
    process.exit(1);
  }
  venv.succeed("Venv ready");

  const py = venvPython();

  // Step 2: pip install into the venv. No --user — we own this interpreter.
  const pip = ora("pip install -r scripts/requirements.txt (into venv)").start();
  const pipResult = await run(py, ["-m", "pip", "install", "-r", requirementsPath]);
  if (!pipResult.ok) {
    pip.fail("pip install failed");
    console.error(chalk.dim(pipResult.stderr.slice(-2000)));
    process.exit(1);
  }
  pip.succeed("Python packages installed");

  // Step 3: playwright install chromium
  const browser = ora("playwright install chromium (one-time, ~150MB)").start();
  const browserResult = await run(py, ["-m", "playwright", "install", "chromium"]);
  if (!browserResult.ok) {
    browser.fail("playwright install failed");
    console.error(chalk.dim(browserResult.stderr.slice(-2000)));
    process.exit(1);
  }
  browser.succeed("Chromium downloaded");

  // Step 4: smoke-test the bridge can at least import everything via the venv
  const smoke = ora("Verifying bridge imports").start();
  const smokeResult = await run(py, [
    "-c",
    "import asyncio, json; from TikTokApi import TikTokApi; print('ok')",
  ]);
  if (!smokeResult.ok || !smokeResult.stdout.includes("ok")) {
    smoke.fail("Bridge import check failed");
    console.error(chalk.dim((smokeResult.stderr || smokeResult.stdout).slice(-2000)));
    process.exit(1);
  }
  smoke.succeed("Bridge ready");

  console.log(chalk.green("\n✓ tiktok-oss is ready.\n"));
  console.log(`Try: ${chalk.cyan("ugcspy search @glossier --platform tiktok")}`);
  console.log(
    chalk.dim(
      "Note: a Chromium window briefly flashes during scrapes — TikTok's bot detection blocks pure headless. See README for MS_TOKEN if you hit rate limits.",
    ),
  );
}

async function findPython(): Promise<string | null> {
  for (const cmd of PYTHON_CANDIDATES) {
    const result = await run(cmd, ["--version"]);
    if (result.ok && /Python 3\.\d+/.test(result.stdout + result.stderr)) {
      return cmd;
    }
  }
  return null;
}

async function run(cmd: string, args: string[]): Promise<RunResult> {
  try {
    const proc = Bun.spawn([cmd, ...args], {
      stdout: "pipe",
      stderr: "pipe",
      env: { ...process.env },
    });
    const [stdout, stderr] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ]);
    const exit = await proc.exited;
    return { ok: exit === 0, stdout, stderr };
  } catch (err) {
    return { ok: false, stdout: "", stderr: (err as Error).message };
  }
}

function resolveRequirements(): string {
  // Same multi-path strategy as src/providers/tiktok-oss.ts resolveScript():
  // dev runs from src/commands/, bundled runs from dist/, npm-installed runs
  // from node_modules/ugcspy/dist/.
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, "..", "..", "scripts", "requirements.txt"),
    resolve(here, "..", "scripts", "requirements.txt"),
  ];
  for (const path of candidates) {
    try {
      const f = Bun.file(path);
      if (f.size > 0) return path;
    } catch {
      // try next
    }
  }
  return candidates[0]!;
}
