import chalk from "chalk";
import prompts from "prompts";
import { CONFIG_PATH, loadConfig, saveConfig } from "../lib/config.ts";
import type { Config } from "../types.ts";

export async function runInit(): Promise<void> {
  console.log(chalk.bold("\nugcspy setup\n"));
  console.log(`Config will be written to ${chalk.dim(CONFIG_PATH)} (chmod 0600).\n`);

  const existing = loadConfig();

  const answers = await prompts(
    [
      {
        type: "select",
        name: "scraper_provider",
        message: "Data provider for TikTok / Instagram",
        choices: [
          { title: "mock (no key — synthetic data for development)", value: "mock" },
          { title: "scrapecreators (recommended for real data — Day 0 spike)", value: "scrapecreators" },
          { title: "apify (stub)", value: "apify", disabled: true },
          { title: "bright_data (stub)", value: "bright_data", disabled: true },
        ],
        initial: providerInitialIndex(existing.scraper_provider),
      },
      {
        type: (prev: string) => (prev === "mock" ? null : "password"),
        name: "scraper_api_key",
        message: "Scraper API key (or leave blank to skip)",
      },
      {
        type: "password",
        name: "anthropic_api_key",
        message: "Anthropic API key (used for hook + format + brief; blank to skip)",
      },
      {
        type: "password",
        name: "openai_api_key",
        message: "OpenAI API key (Whisper fallback only; blank to skip)",
      },
      {
        type: "text",
        name: "default_slack_webhook",
        message: "Default Slack webhook URL for alerts (blank to skip)",
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
    anthropic_api_key: answers.anthropic_api_key || existing.anthropic_api_key,
    openai_api_key: answers.openai_api_key || existing.openai_api_key,
    default_slack_webhook: answers.default_slack_webhook || existing.default_slack_webhook,
  };

  saveConfig(next);
  console.log(chalk.green(`\n✓ Config saved.`));
  console.log(`Run ${chalk.cyan("ugcspy search @glossier")} to try a search.`);
}

function providerInitialIndex(p: string | undefined): number {
  switch (p) {
    case "mock":
      return 0;
    case "scrapecreators":
      return 1;
    default:
      return 0;
  }
}
