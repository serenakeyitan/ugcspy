import chalk from "chalk";
import { InstagramOssProvider } from "../providers/instagram-oss.ts";

// `ugcspy ig-session` — surface the Instagram session health the IG fetch path
// depends on. The IG bridge reads a logged-in browser session (default safari)
// for gallery-dl + instaloader; when the session is missing/expired every IG
// fetch fails with a re-login error. This command lets the user check + fix it
// BEFORE a watch silently stops firing.
export async function runIgSession(): Promise<void> {
  const provider = new InstagramOssProvider();
  let status: { loggedIn: boolean; igCookieCount: number; browser: string };
  try {
    status = await provider.sessionCheck();
  } catch (err) {
    console.error(chalk.red(`Could not check the Instagram session: ${(err as Error).message}`));
    console.log(
      chalk.dim(
        "Run `ugcspy install-deps` first if you haven't (it installs the gallery-dl + instaloader deps).",
      ),
    );
    process.exit(1);
  }

  if (status.loggedIn) {
    console.log(
      chalk.green(
        `✓ Logged into Instagram in ${chalk.cyan(status.browser)} (${status.igCookieCount} IG cookies, session present).`,
      ),
    );
    console.log(chalk.dim("Instagram search / scout / watch will work."));
  } else {
    console.log(
      chalk.yellow(
        `✗ No logged-in Instagram session in ${chalk.cyan(status.browser)} (${status.igCookieCount} IG cookies, but no sessionid).`,
      ),
    );
    console.log("To fix:");
    console.log(`  1. Open ${chalk.cyan(status.browser)} and log into instagram.com.`);
    console.log(
      `  2. Re-run ${chalk.cyan("ugcspy ig-session")} to confirm — or point ugcspy at a different browser with ${chalk.cyan("UGCSPY_IG_COOKIE_BROWSER=chrome")} (chrome | firefox | safari).`,
    );
    console.log(
      chalk.dim(
        "Instagram sessions expire periodically; if IG watches stop firing, re-check here.",
      ),
    );
    process.exit(1);
  }
}
