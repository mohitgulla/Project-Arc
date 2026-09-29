import { defineConfig } from "@playwright/test";

// E2E: the shell and /kitchen-sink at the three TOWER_DESIGN §6 viewports, both themes.
// `make web-e2e` builds the SPA, then Playwright starts `arc tower serve --v2 --local`
// on a scratch DB (e2e/serve.mjs). Screenshots land in e2e/screenshots/ for the PR.
const port = process.env.ARC_E2E_PORT ?? "4181";
const baseURL = process.env.ARC_E2E_URL ?? `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL,
    timezoneId: "America/New_York",
    locale: "en-US",
    // Full Chromium in new-headless mode (no separate headless-shell download needed).
    channel: "chromium",
  },
  webServer: process.env.ARC_E2E_URL
    ? undefined
    : {
        command: "node e2e/serve.mjs",
        url: `${baseURL}/api/health`,
        reuseExistingServer: false,
        timeout: 60_000,
      },
});
