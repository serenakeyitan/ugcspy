import chalk from "chalk";
import prompts from "prompts";
import { CONFIG_PATH, loadConfig, saveConfig } from "../lib/config.ts";
import type { Config } from "../types.ts";

export type Provider = "tiktok-oss" | "scrapecreators" | "mock";

export interface InitOptions {
  // Non-interactive auto-accept mode for plugin/onboarding use.
  // When yes=true, runs without prompts using the values below + defaults
  // (provider=tiktok-oss, no scraper key, no slack webhook).
  yes?: boolean;
  provider?: Provider;
  scraperApiKey?: string;
  slackWebhook?: string;
}

export async function runInit(opts: InitOptions = {}): Promise<void> {
  console.log(chalk.bold("\nugcspy setup\n"));
  console.log(`Config will be written to ${chalk.dim(CONFIG_PATH)} (chmod 0600).\n`);

  const existing = loadConfig();

  if (opts.yes) {
    // Non-interactive mode: take everything from flags + sensible defaults.
    const provider = opts.provider ?? "tiktok-oss";
    if (provider === "scrapecreators" && !opts.scraperApiKey) {
      console.log(
        chalk.yellow(
          `Provider=scrapecreators selected but no --scraper-api-key passed — saving without a key (you can re-run init later to add one).`,
        ),
      );
    }
    const next: Config = {
      scraper_provider: provider,
      scraper_api_key: opts.scraperApiKey ?? existing.scraper_api_key,
      default_slack_webhook: opts.slackWebhook ?? existing.default_slack_webhook,
    };
    saveConfig(next);
    console.log(chalk.green(`\n✓ Config saved (non-interactive, provider=${provider}).`));
    printNextStep(next);
    return;
  }

  // Interactive flow (unchanged).
  const answers = await prompts(
    [
      {
        type: "select",
        name: "scraper_provider",
        message: "Data provider",
        choices: [
          {
            title: "tiktok-oss — free, TikTok only (davidteather/TikTok-Api via Python)",
            value: "tiktok-oss",
          },
          { title: "scrapecreators — paid, TikTok + Instagram Reels", value: "scrapecreators" },
          { title: "mock — synthetic data, no setup needed", value: "mock" },
          { title: "apify (stub)", value: "apify", disabled: true },
          { title: "bright_data (stub)", value: "bright_data", disabled: true },
        ],
        initial: providerInitialIndex(existing.scraper_provider),
      },
      {
        type: (prev: string) => (prev === "scrapecreators" ? "password" : null),
        name: "scraper_api_key",
        message: "ScrapeCreators API key (or leave blank to skip)",
      },
      {
        type: "text",
        name: "default_slack_webhook",
        message: "Default Slack webhook URL for optional alerts (blank to skip)",
      },
    ],
    {
      onCancel: () => {
        console.log(chalk.yellow("\nSetup cancelled — no changes written."));
        process.exit(1);
      },
    },
  );

  const next: Config = {
    scraper_provider: answers.scraper_provider,
    scraper_api_key: answers.scraper_api_key || existing.scraper_api_key,
    default_slack_webhook: answers.default_slack_webhook || existing.default_slack_webhook,
  };

  saveConfig(next);
  console.log(chalk.green(`\n✓ Config saved.`));
  printNextStep(next);
}

function printNextStep(config: Config): void {
  if (config.scraper_provider === "tiktok-oss") {
    console.log(
      chalk.yellow(
        "\nThe tiktok-oss provider needs Python + TikTokApi + Chromium installed locally.",
      ),
    );
    console.log(`Run ${chalk.cyan("ugcspy install-deps")} now (one-time, ~30s + ~150MB download).`);
    console.log(
      chalk.dim(
        `  If you'll use ${chalk.cyan("/ugcspy-decode")} or ${chalk.cyan("/ugcspy-remix")} for AI-style remixing,`,
      ),
    );
    console.log(
      chalk.dim(
        `  add ${chalk.cyan("--with-audio")} (Whisper for spoken-narrative capture; ~3-5min + ~1.5GB).`,
      ),
    );
    console.log(`Then: ${chalk.cyan("ugcspy search befreed --platform tiktok")}`);
  } else {
    console.log(`Run ${chalk.cyan("ugcspy search befreed")} to try a search.`);
  }
}

function providerInitialIndex(p: string | undefined): number {
  switch (p) {
    case "tiktok-oss":
      return 0;
    case "scrapecreators":
      return 1;
    case "mock":
      return 2;
    default:
      return 0;
  }
}
