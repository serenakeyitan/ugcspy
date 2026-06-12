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

export interface InstallDepsOptions {
  /** When true, ALSO installs openai-whisper + torch (~1.5GB) into the
   * venv. Needed by /ugcspy-decode + /ugcspy-remix for spoken-audio
   * transcription. decode.py degrades gracefully without it.*/
  withAudio?: boolean;
  /** When true, ALSO downloads the Chromium binary (~150MB) via playwright.
   * Only the optional browser-assisted fallbacks use it (UGCSPY_USE_CHROMIUM=1
   * discovery, legacy TikTokApi user-mode fallback). The default hashtag
   * search is browser-free and never touches it. */
  withBrowser?: boolean;
}

export async function runInstallDeps(opts: InstallDepsOptions = {}): Promise<void> {
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

  // Step 3 (opt-in): Chromium binary for the browser-assisted fallbacks.
  // The default hashtag search is browser-free (tikwm relay + yt-dlp), so a
  // 150MB browser download must not be able to block a fresh install.
  if (opts.withBrowser) {
    const browser = ora("playwright install chromium (one-time, ~150MB)").start();
    const browserResult = await run(py, ["-m", "playwright", "install", "chromium"]);
    if (!browserResult.ok) {
      browser.fail("playwright install failed");
      console.error(chalk.dim(browserResult.stderr.slice(-2000)));
      process.exit(1);
    }
    browser.succeed("Chromium downloaded");
  } else {
    console.log(
      chalk.dim(
        "Skipped Chromium download (~150MB) — the default hashtag search is browser-free.\n" +
          "If you later enable the browser-assisted fallback (UGCSPY_USE_CHROMIUM=1), first run: " +
          chalk.cyan("ugcspy install-deps --with-browser"),
      ),
    );
  }

  // Step 4: smoke-test the bridge can at least import everything via the venv.
  // yt_dlp is included because the default Stage-2 coverage walk shells out to
  // the venv's yt-dlp binary — a missing module here means zero search results.
  const smoke = ora("Verifying bridge imports").start();
  const smokeResult = await run(py, [
    "-c",
    "import asyncio, json, yt_dlp; from TikTokApi import TikTokApi; print('ok')",
  ]);
  if (!smokeResult.ok || !smokeResult.stdout.includes("ok")) {
    smoke.fail("Bridge import check failed");
    console.error(chalk.dim((smokeResult.stderr || smokeResult.stdout).slice(-2000)));
    process.exit(1);
  }
  smoke.succeed("Bridge ready");

  // Step 5 (opt-in): audio transcription deps. Big — ~1.5GB. Only
  // installed when the user explicitly asks via --with-audio.
  if (opts.withAudio) {
    const audioReqPath = resolveAudioRequirements();
    if (!existsSync(audioReqPath)) {
      console.error(chalk.red(`✖ Audio requirements file missing: ${audioReqPath}`));
      process.exit(1);
    }
    const audio = ora("pip install -r scripts/requirements-audio.txt (whisper + torch, ~1.5GB, slow)").start();
    const audioResult = await run(py, ["-m", "pip", "install", "-r", audioReqPath]);
    if (!audioResult.ok) {
      audio.fail("Audio packages install failed");
      console.error(chalk.dim(audioResult.stderr.slice(-2000)));
      process.exit(1);
    }
    audio.succeed("Audio packages installed (whisper + torch)");
    // Smoke-test the audio path — imageio_ffmpeg too: it bundles the static
    // ffmpeg the transcript pipeline uses, so NO system ffmpeg is needed.
    const audioSmoke = ora("Verifying whisper + bundled ffmpeg").start();
    const audioSmokeResult = await run(py, [
      "-c",
      "import whisper, imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe(); print('ok')",
    ]);
    if (!audioSmokeResult.ok || !audioSmokeResult.stdout.includes("ok")) {
      audioSmoke.fail("Whisper / bundled-ffmpeg check failed");
      console.error(chalk.dim((audioSmokeResult.stderr || audioSmokeResult.stdout).slice(-2000)));
      process.exit(1);
    }
    audioSmoke.succeed("Whisper + bundled ffmpeg ready");

    // Pre-download the Whisper model NOW (it caches in ~/.cache/whisper), so
    // the user's first `ugcspy transcript` isn't a silent multi-minute model
    // download behind a spinner. Non-fatal: a network blip here just defers
    // the download back to first use.
    const modelName = (process.env.UGCSPY_WHISPER_MODEL ?? "base").trim() || "base";
    const model = ora(`Pre-downloading the Whisper '${modelName}' model (~140MB, one-time)`).start();
    const modelResult = await run(py, [
      "-c",
      `import whisper; whisper.load_model(${JSON.stringify(modelName)}); print('ok')`,
    ]);
    if (modelResult.ok && modelResult.stdout.includes("ok")) {
      model.succeed(`Whisper '${modelName}' model cached`);
    } else {
      model.warn(
        `Model pre-download failed — the first transcript will download it instead. (${(modelResult.stderr || "").trim().slice(-200)})`,
      );
    }
  }

  console.log(chalk.green("\n✓ tiktok-oss is ready.\n"));
  console.log(`Try: ${chalk.cyan("ugcspy search liquiddeath --platform tiktok --limit 10")}`);
  console.log(
    chalk.dim(
      "Hashtag search runs browser-free (tikwm relay + yt-dlp). The first search on an active brand takes a few minutes while Stage 2 walks every discovered creator's catalog; results are cached after that, ranked by views.",
    ),
  );
  if (opts.withAudio) {
    console.log(chalk.dim("\nAudio transcription enabled. /ugcspy-decode + /ugcspy-remix will use Whisper for spoken-narrative capture (口型 / lip-sync source)."));
  } else {
    console.log(
      chalk.dim(
        "\nWithout --with-audio, /ugcspy-decode + /ugcspy-remix work but only see on-screen overlay text. To add spoken-audio capture (~3-5min + ~1.5GB): re-run with " +
          chalk.cyan("ugcspy install-deps --with-audio"),
      ),
    );
  }
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

// Exported for tests: a scripts/ rename or move must fail in CI, not on a
// user's machine mid-onboarding.
export function resolveRequirements(): string {
  return resolveScriptsFile("requirements.txt");
}

export function resolveAudioRequirements(): string {
  return resolveScriptsFile("requirements-audio.txt");
}

function resolveScriptsFile(name: string): string {
  // Same multi-path strategy as src/providers/tiktok-oss.ts resolveScript():
  // dev runs from src/commands/, bundled runs from dist/, npm-installed runs
  // from node_modules/ugcspy/dist/.
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, "..", "..", "scripts", name),
    resolve(here, "..", "scripts", name),
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
