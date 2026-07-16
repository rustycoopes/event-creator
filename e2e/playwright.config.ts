import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the Event Creator E2E suite (Slice R13).
 *
 * These tests drive the REAL deployed QA app end-to-end - there is no local web server started
 * here. `PLAYWRIGHT_BASE_URL` must point at the shared Load Balancer's custom domain (set in CI
 * after `deploy-qa` succeeds), not Event Creator's own Cloud Run URL directly - relative
 * `hx-get`/fetch calls (e.g. Settings' Storage/Notifications fragments, R7) resolve against
 * whatever origin the page was loaded from, and only the LB's URL map
 * (infra/gcp_lb/generate_url_map.py) knows how to route those paths to Event Creator's own Cloud
 * Run service. Hitting Event Creator's own Cloud Run URL directly bypasses that routing entirely.
 * Falls back to the known QA domain for convenient local runs.
 */
const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ?? 'https://organizeme.qa.russcoopersoftware.com';

export default defineConfig({
  testDir: './tests',
  // Unique-per-run emails mean tests never collide, so they can run fully in parallel.
  fullyParallel: true,
  // Fail the CI build if a `test.only` was left in the source.
  forbidOnly: !!process.env.CI,
  // A remote target can have transient blips; one retry in CI keeps flakes from failing the run.
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [['list'], ['html', { open: 'never' }]],
  // Registration + login each do a (deliberately slow) bcrypt hash plus a Supabase round-trip,
  // and several flows chain register -> login -> redirect, so give assertions and whole tests
  // generous ceilings rather than the tight 5s/30s defaults.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    // Capture a trace + screenshot only when a test fails and is retried, to keep artifacts small.
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
