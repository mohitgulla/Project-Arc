import { defineConfig } from "@playwright/test";

import { PHONE_75_DEVICE } from "./e2e/mobile";

// E2E: the shell and /kitchen-sink at the three TOWER_DESIGN §6 viewports, both themes,
// against an empty scratch DB; the Overview (E8.7a) against the populated fixture DB
// (scripts/tower_fixture_db.py, see e2e/overview.spec.ts); the Performance page (E8.7c)
// against the fixture plus its performance history (--history, e2e/performance.spec.ts);
// the Ops page (E8.7d) against the fixture plus its ops rows (--ops, e2e/ops.spec.ts);
// the E8.7e cutover smoke over all five pages on the same --ops store (e2e/cutover.spec.ts).
// `make web-e2e` builds the SPA,
// then Playwright starts `arc tower serve --local` on each (e2e/serve.mjs).
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
  // `default` keeps the TOWER_DESIGN §6 viewports (390/768/1440) and the behaviour tests.
  // `phone-75` (E8.8a, D48) is the owner's iPhone Safari at 75 % zoom: 520x1125, touch, iPhone
  // UA. It runs only tests tagged `@phone-75` (the project name itself is part of every test's
  // grep title, so the tag carries the `@`); see e2e/mobile.ts.
  // Engine: Chromium (WebKit download hangs extracting on the dev host, as chromium did in E8.7d).
  projects: [
    { name: "default", grepInvert: /@phone-75/ },
    { name: "phone-75", grep: /@phone-75/, use: { ...PHONE_75_DEVICE } },
  ],
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
