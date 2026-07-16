import { test, expect } from '@playwright/test';
import { registerNewUser, uploadFileAndWaitForCompletion } from '../utils/helpers';

/**
 * Slice R11 (#166) — the events dashboard had no dedicated e2e coverage (only backend/httpx
 * tests in the event-creator repo, plus sidebar.spec.ts's heading-only nav check). Added while
 * closing the R11 verification gap for PRD stories 32-39.
 *
 * Drives a real upload through the deployed QA app (E2E_TEST_MODE's fake Gemini client always
 * returns the same two-event canned payload - "E2E test — pick up from school." (School) and
 * "E2E test — swim meet." (Activity), see app.services.llm.gemini.E2E_FAKE_EXTRACTION_PAYLOAD in
 * the event-creator repo - deterministic events to assert against, no need to fabricate fixture
 * data directly against the DB) and waits for the pipeline to finish, matching
 * processing.spec.ts's proven pattern (via e2e/utils/helpers.ts), then exercises the dashboard's
 * table/filter/sort/delete.
 */
test.describe('Events dashboard', () => {
  test('shows extracted events with calendar/tasks links and initials chips', async ({ page }) => {
    await registerNewUser(page, 'dashboard-table');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');

    await page.goto('/dashboard');

    await expect(page.getByText('E2E test — pick up from school.')).toBeVisible();
    await expect(page.getByText('E2E test — swim meet.')).toBeVisible();
    await expect(page.getByText('2 events total')).toBeVisible();

    const row = page.locator('#events-table tbody tr', {
      hasText: 'E2E test — pick up from school.',
    });
    // Calendar/Tasks quick-add links (Google's own domains, opened in a new tab).
    await expect(row.getByRole('link', { name: 'Add' }).first()).toHaveAttribute(
      'href',
      /^https:\/\/calendar\.google\.com\//,
    );
    // "Test Parent A" / "Test Parent B" -> initials chips "TA"/"TB".
    await expect(row.getByText('TA', { exact: true })).toBeVisible();
    await expect(row.getByText('TB', { exact: true })).toBeVisible();
  });

  test('type filter narrows the table without a full page reload', async ({ page }) => {
    await registerNewUser(page, 'dashboard-filter');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    await page.locator('#filter-type').selectOption('School');

    await expect(page.getByText('E2E test — pick up from school.')).toBeVisible();
    await expect(page.getByText('E2E test — swim meet.')).not.toBeVisible();
    // hx-push-url keeps the URL in sync with the HTMX-driven filter.
    await expect(page).toHaveURL(/type=School/);
  });

  test('sort toggle reverses the events order', async ({ page }) => {
    await registerNewUser(page, 'dashboard-sort');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    await expect(page.locator('#events-table tbody tr').first()).toContainText('swim meet');

    await page.getByRole('link', { name: /^Sort:/ }).click();

    await expect(page).toHaveURL(/sort=asc/);
    await expect(page.locator('#events-table tbody tr').first()).toContainText(
      'pick up from school',
    );
  });

  test('delete removes an event behind a confirm dialog', async ({ page }) => {
    await registerNewUser(page, 'dashboard-delete');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    const row = page.locator('#events-table tbody tr', { hasText: 'swim meet' });
    await row.getByRole('button', { name: 'Delete' }).click();

    // Confirm dialog gates the delete - it must not happen on the first click.
    await expect(page.getByText('E2E test — swim meet.')).toBeVisible();
    await page.locator('dialog.modal button.btn-error').click();

    await expect(page.getByText('E2E test — swim meet.')).not.toBeVisible();
    await expect(page.getByText('E2E test — pick up from school.')).toBeVisible();
    await expect(page.getByText('1 event total')).toBeVisible();
  });
});
