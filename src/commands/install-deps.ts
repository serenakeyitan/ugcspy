import chalk from "chalk";
import ora from "ora";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const PYTHON_CANDIDATES = ["python3", "python"] as const;

interface RunResult {
  ok: boolean;
  stdout: string;
  stderr: string;
}

export async function runInstallDeps(): Promise<void> {
  console.log(chalk.bold("\nInstalling tiktok-oss provider dependencies...\n"));

  const python = await findPython();
  if (!python) {
    console.error(chalk.red("✖ Python 3 not found on PATH."));
    console.error(`  Install Python 3.9+ first: ${chalk.cyan("https://www.python.org/downloads/")}`);
    process.exit(1);
  }
  console.log(chalk.dim(`Using ${python}.\n`));

  const requirementsPath = resolveRequirements();
  if (!existsSync(requirementsPath)) {
    console.error(chalk.red(`✖ Requirements file missing: ${requirementsPath}`));
    process.exit(1);
  }

  // Step 1: pip install --user (avoids sudo, doesn't pollute system Python)
  const pip = ora("pip install -r scripts/requirements.txt --user").start();
  const pipResult = await run(python, ["-m", "pip", "install", "--user", "-r", requirementsPath]);
  if (!pipResult.ok) {
    pip.fail("pip install failed");
    console.error(chalk.dim(pipResult.stderr.slice(-2000)));
    process.exit(1);
  }
  pip.succeed("Python packages installed");

  // Step 2: playwright install chromium
  const browser = ora("playwright install chromium (one-time, ~150MB)").start();
  const browserResult = await run(python, ["-m", "playwright", "install", "chromium"]);
  if (!browserResult.ok) {
    browser.fail("playwright install failed");
    console.error(chalk.dim(browserResult.stderr.slice(-2000)));
    process.exit(1);
  }
  browser.succeed("Chromium downloaded");

  // Step 3: smoke-test the bridge can at least import everything
  const smoke = ora("Verifying bridge imports").start();
  const smokeResult = await run(python, [
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
  const here = dirname(fileURLToPath(import.meta.url));
  // src/commands/ -> ../../scripts/requirements.txt
  return resolve(here, "..", "..", "scripts", "requirements.txt");
}
