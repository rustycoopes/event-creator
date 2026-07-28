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
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();

    await expect(page.getByText('E2E test — swim meet.')).not.toBeVisible();
    await expect(page.getByText('E2E test — pick up from school.')).toBeVisible();
    await expect(page.getByText('1 event total')).toBeVisible();
  });

  // event-creator#41 (dashboard-bulk-actions Slice 1): the two-event canned fixture is enough to
  // exercise the header checkbox's indeterminate state (one selected out of two is already "some
  // but not all") without needing a bigger, hand-rolled fixture.
  test('row/select-all checkboxes drive the bulk toolbar and header indeterminate state', async ({
    page,
  }) => {
    await registerNewUser(page, 'dashboard-bulk-select');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    const headerCheckbox = page.getByRole('checkbox', { name: 'Select all events on this page' });
    const schoolRow = page.locator('#events-table tbody tr', { hasText: 'pick up from school' });
    const swimRow = page.locator('#events-table tbody tr', { hasText: 'swim meet' });

    await expect(page.getByText(/^\d+ selected$/)).not.toBeVisible();

    await schoolRow.getByRole('checkbox', { name: 'Select event' }).check();

    await expect(page.getByText('1 selected')).toBeVisible();
    await expect(headerCheckbox).not.toBeChecked();
    expect(await headerCheckbox.evaluate((el: HTMLInputElement) => el.indeterminate)).toBe(true);

    await swimRow.getByRole('checkbox', { name: 'Select event' }).check();

    await expect(page.getByText('2 selected')).toBeVisible();
    await expect(headerCheckbox).toBeChecked();
    expect(await headerCheckbox.evaluate((el: HTMLInputElement) => el.indeterminate)).toBe(false);

    await headerCheckbox.uncheck();

    await expect(page.getByText(/^\d+ selected$/)).not.toBeVisible();
  });

  test('bulk delete removes only the selected events behind a confirm dialog showing the count, then clears selection', async ({
    page,
  }) => {
    await registerNewUser(page, 'dashboard-bulk-delete');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    const swimRow = page.locator('#events-table tbody tr', { hasText: 'swim meet' });
    await swimRow.getByRole('checkbox', { name: 'Select event' }).check();
    await page.getByRole('button', { name: 'Delete Selected' }).click();

    // Confirm dialog gates the delete and states the exact count - it must not happen on the
    // first click.
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Delete 1 event?')).toBeVisible();
    await expect(page.getByText('E2E test — swim meet.')).toBeVisible();

    await dialog.getByRole('button', { name: 'Delete' }).click();

    await expect(page.getByText('E2E test — swim meet.')).not.toBeVisible();
    await expect(page.getByText('E2E test — pick up from school.')).toBeVisible();
    await expect(page.getByText('1 event total')).toBeVisible();
    // Selection clears for free once the table refreshes (event-creator#41 TDD) - the toolbar
    // and header checkbox both go back to their empty state.
    await expect(page.getByText(/^\d+ selected$/)).not.toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Select all events on this page' })).not.toBeChecked();
  });

  // event-creator#42 (dashboard-bulk-actions Slice 2): "Mark Selected as Reviewed" applies with
  // no confirm dialog (unlike bulk delete), styled distinctly (primary/cobalt) from Delete
  // Selected (danger-solid/flame).
  test('bulk mark-reviewed applies immediately with no confirm dialog, hides marked rows, and is styled distinctly from Delete Selected', async ({
    page,
  }) => {
    await registerNewUser(page, 'dashboard-bulk-review');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    const swimRow = page.locator('#events-table tbody tr', { hasText: 'swim meet' });
    await swimRow.getByRole('checkbox', { name: 'Select event' }).check();

    const markReviewedButton = page.getByRole('button', { name: 'Mark Selected as Reviewed' });
    const deleteSelectedButton = page.getByRole('button', { name: 'Delete Selected' });
    await expect(markReviewedButton).toHaveClass(/bg-cobalt/);
    await expect(deleteSelectedButton).toHaveClass(/bg-flame/);

    await markReviewedButton.click();

    // Applies immediately - no confirm dialog gates it, unlike bulk delete.
    await expect(page.getByRole('dialog')).not.toBeVisible();
    await expect(page.getByText('E2E test — swim meet.')).not.toBeVisible();
    await expect(page.getByText('E2E test — pick up from school.')).toBeVisible();
    await expect(page.getByText('1 event total')).toBeVisible();
    // Selection clears for free once the table refreshes, same as bulk delete.
    await expect(page.getByText(/^\d+ selected$/)).not.toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Select all events on this page' })).not.toBeChecked();
  });

  test('selection clears after a filter change and after a sort change', async ({ page }) => {
    await registerNewUser(page, 'dashboard-bulk-clear');
    await uploadFileAndWaitForCompletion(page, 'chat.txt', 'E2E dashboard test conversation.\n');
    await page.goto('/dashboard');

    const headerCheckbox = page.getByRole('checkbox', { name: 'Select all events on this page' });
    await headerCheckbox.check();
    await expect(page.getByText('2 selected')).toBeVisible();

    await page.locator('#filter-type').selectOption('School');
    await expect(page.getByText(/^\d+ selected$/)).not.toBeVisible();

    await page.getByRole('checkbox', { name: 'Select all events on this page' }).check();
    await expect(page.getByText('1 selected')).toBeVisible();

    await page.getByRole('link', { name: /^Sort:/ }).click();
    await expect(page.getByText(/^\d+ selected$/)).not.toBeVisible();
  });
});
