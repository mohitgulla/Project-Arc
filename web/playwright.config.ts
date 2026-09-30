import { defineConfig } from "@playwright/test";

// E2E: the shell and /kitchen-sink at the three TOWER_DESIGN §6 viewports, both themes,
// against an empty scratch DB; the Overview (E8.7a) against the populated fixture DB
// (scripts/tower_fixture_db.py, see e2e/overview.spec.ts); the Performance page (E8.7c)
// against the fixture plus its performance history (--history, e2e/performance.spec.ts);
// the Ops page (E8.7d) against the fixture plus its ops rows (--ops, e2e/ops.spec.ts).
// `make web-e2e` builds the SPA,
// then Playwright starts `arc tower serve --v2 --local` on each (e2e/serve.mjs).
// Screenshots land in e2e/screenshots/ for the PR.
const port = process.env.ARC_E2E_PORT ?? "4181";
const fixturePort = process.env.ARC_E2E_FIXTURE_PORT ?? "4182";
const baseURL = process.env.ARC_E2E_URL ?? `http://127.0.0.1:${port}`;
const fixtureURL = process.env.ARC_E2E_FIXTURE_URL ?? `http://127.0.0.1:${fixturePort}`;
const historyPort = process.env.ARC_E2E_HISTORY_PORT ?? "4183";
const historyURL = process.env.ARC_E2E_HISTORY_URL ?? `http://127.0.0.1:${historyPort}`;
const opsPort = process.env.ARC_E2E_OPS_PORT ?? "4184";
const opsURL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${opsPort}`;

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
    : [
        {
          command: "node e2e/serve.mjs",
          url: `${baseURL}/api/health`,
          reuseExistingServer: false,
          timeout: 60_000,
        },
        {
          command: "node e2e/serve.mjs --fixture",
          url: `${fixtureURL}/api/health`,
          reuseExistingServer: false,
          timeout: 60_000,
        },
        {
          command: "node e2e/serve.mjs --history",
          url: `${historyURL}/api/health`,
          reuseExistingServer: false,
          timeout: 60_000,
        },
        {
          command: "node e2e/serve.mjs --ops",
          url: `${opsURL}/api/health`,
          reuseExistingServer: false,
          timeout: 90_000,
        },
      ],
});
