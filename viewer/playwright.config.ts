import { defineConfig, devices } from "@playwright/test";

// T022 (US4) Playwright config for the interaction/perf spec. It drives a
// RUNNING dev server (default http://localhost:5176 — the one `flux serve`
// boots), so it does NOT start its own server. Point it elsewhere with
// PLAYWRIGHT_BASE_URL. Browsers come from the shared ~/.cache/ms-playwright.
export default defineConfig({
  testDir: "./tests",
  testMatch: /.*\.spec\.ts/,
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5176",
    headless: true,
    trace: "off",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
