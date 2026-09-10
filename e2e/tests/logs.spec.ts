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

// mobile-responsive-tables Slice 2 (#50): post-deploy regression guard for the phone layout -
// the runs grid flips to `.om-stacked-table` cards and the filter/sort form collapses behind a
// "Filters (N)" toggle below lg (1024px). A 375px viewport override, matching the pattern
// dashboard.spec.ts adds in Slice 1b.
test.describe('Logs (processing history) — mobile viewport', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test('runs grid renders as labelled cards with no horizontal document scroll', async ({
    page,
  }) => {
    await registerNewUser(page, 'logs-mobile-cards');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E logs mobile test conversation.\n');
    await page.goto('/logs');

    await expect(page.locator('#logs-grid tbody tr', { hasText: 'chat.txt' })).toBeVisible();

    // Card mode is active: the <table> is laid out as blocks, not a table.
    await expect(page.locator('#logs-grid')).toHaveCSS('display', 'block');

    // The data-label shows as the cell's ::before prefix (e.g. "Filename: ").
    const label = await page
      .locator('#logs-grid td[data-label="Filename"]')
      .first()
      .evaluate((el) => window.getComputedStyle(el, '::before').content);
    expect(label).toContain('Filename');

    // No horizontal document scroll.
    const overflowsX = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflowsX).toBe(false);
  });

  test('filter form is collapsed until "Filters" is tapped and shows the active count', async ({
    page,
  }) => {
    await registerNewUser(page, 'logs-mobile-filters');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E logs mobile filter conversation.\n');
    await page.goto('/logs');

    const filterForm = page.locator('#run-filters');
    await expect(filterForm).toBeHidden();

    // Keyboard-operable: the sr-only toggle checkbox takes focus and Space opens the panel.
    await page.locator('#run-filters-disclosure').press(' ');
    await expect(filterForm).toBeVisible();
    await page.locator('#run-filters-disclosure').press(' ');
    await expect(filterForm).toBeHidden();

    await page.getByText('Filters (0)').click();
    await expect(filterForm).toBeVisible();

    // Below lg the grid's <thead> sort links are visually hidden by .om-stacked-table, so the
    // sort control lives in the disclosure panel instead - it must be reachable and it re-sorts.
    await expect(page.locator('#mobile-sort-by')).toBeVisible();
    await page.locator('#mobile-sort-by').selectOption('filename');
    await expect(page).toHaveURL(/sort_by=filename/);

    // The count re-renders with the fragment on every swap.
    await page.locator('#filter-status').selectOption('success');
    await expect(page).toHaveURL(/status=success/);
    await expect(page.getByText('Filters (1)')).toBeVisible();
  });

  test('tapping a run card navigates to its detail page', async ({ page }) => {
    await registerNewUser(page, 'logs-mobile-nav');
    await uploadFileAndWaitForCompletion(page, 'nav-chat.txt', 'E2E logs mobile nav conversation.\n');
    await page.goto('/logs');

    await page.locator('#logs-grid tbody tr', { hasText: 'nav-chat.txt' }).click();

    await expect(page).toHaveURL(/\/processing-runs\/[0-9a-f-]+$/);
    await expect(page.getByText('nav-chat.txt')).toBeVisible();
  });
});
