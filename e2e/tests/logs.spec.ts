import { test, expect } from '@playwright/test';
import { registerNewUser, uploadFileAndWaitForCompletion } from '../utils/helpers';

/**
 * Slice R11 (#166) — the Processing History / Logs grid had no dedicated e2e coverage. Added
 * while closing the R11 verification gap for PRD stories 30-31.
 *
 * Reuses the same upload -> wait-for-success pattern as processing.spec.ts/dashboard.spec.ts;
 * E2E_TEST_MODE's fake Gemini client deterministically extracts 2 events, so a completed run
 * always shows "2" in the grid's Events column.
 */
test.describe('Logs (processing history)', () => {
  test('lists a completed run in the grid with its status and event count', async ({ page }) => {
    await registerNewUser(page, 'logs-grid');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E logs test conversation.\n');

    await page.goto('/logs');

    const row = page.locator('#logs-grid tbody tr', { hasText: 'chat.txt' });
    await expect(row).toBeVisible();
    await expect(row.getByText('success')).toBeVisible();
    await expect(row).toContainText('2');
  });

  test('clicking a run row navigates to its detail page with searchable logs', async ({
    page,
  }) => {
    await registerNewUser(page, 'logs-detail');
    await uploadFileAndWaitForCompletion(page, 'detail-chat.txt', 'E2E logs detail test conversation.\n');

    await page.goto('/logs');
    await page.locator('#logs-grid tbody tr', { hasText: 'detail-chat.txt' }).click();

    await expect(page).toHaveURL(/\/processing-runs\/[0-9a-f-]+$/);
    await expect(page.getByText('detail-chat.txt')).toBeVisible();
    // Every step reached a terminal state, matching the completed run.
    for (let n = 1; n <= 7; n++) {
      await expect(page.locator(`#step-${n}`)).toBeVisible();
    }
  });

  test('status filter narrows the grid to matching runs', async ({ page }) => {
    await registerNewUser(page, 'logs-filter');
    await uploadFileAndWaitForCompletion(page, 'filter-chat.txt', 'E2E logs filter test conversation.\n');

    await page.goto('/logs');
    await page.locator('#filter-status').selectOption('success');

    await expect(page.locator('#logs-grid tbody tr', { hasText: 'filter-chat.txt' })).toBeVisible();
    await expect(page).toHaveURL(/status=success/);

    await page.locator('#filter-status').selectOption('failed');

    await expect(
      page.locator('#logs-grid tbody tr', { hasText: 'filter-chat.txt' }),
    ).not.toBeVisible();
  });
});
