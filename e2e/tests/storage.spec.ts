import { test, expect } from '@playwright/test';
import { registerNewUser } from '../utils/helpers';

test.describe('Settings > Storage', () => {
  test('conditional fields show/hide based on the selected provider', async ({ page }) => {
    await registerNewUser(page, 'storage');
    await page.goto('/settings');

    const folderPath = page.locator('#folder_path');
    const s3Stub = page.getByText('Amazon S3 support is coming soon.');

    // Google Drive is the default: the shared folder field is visible, the S3 stub isn't.
    await expect(folderPath).toBeVisible();
    await expect(s3Stub).toBeHidden();

    // Dropbox (R7) reuses the same folder field as Google Drive - only S3 hides it.
    await page.locator('#provider').selectOption('dropbox');
    await expect(folderPath).toBeVisible();
    await expect(s3Stub).toBeHidden();

    // Selecting S3 hides the folder field and reveals its stub - no page reload.
    await page.locator('#provider').selectOption('s3');
    await expect(folderPath).toBeHidden();
    await expect(s3Stub).toBeVisible();

    // Back to Google Drive restores the shared folder field.
    await page.locator('#provider').selectOption('google_drive');
    await expect(folderPath).toBeVisible();
    await expect(s3Stub).toBeHidden();
  });

  test('Connect Google Drive control appears once a folder path is saved', async ({ page }) => {
    await registerNewUser(page, 'storage-connect');
    await page.goto('/settings');

    // A brand-new user has no saved config: Connect is gated behind saving a folder path first.
    const connectButton = page.locator('#connect-drive');
    await expect(connectButton).toBeHidden();
    // Full text (not the shared "Save your folder path first" substring) - Dropbox's Connect
    // panel has the same lead-in and is present (if hidden) in the DOM at the same time, so a
    // partial-text locator resolves to both and violates Playwright's strict mode.
    await expect(
      page.getByText('Save your folder path first, then connect Google Drive.')
    ).toBeVisible();

    await page.locator('#folder_path').fill('/OrganizeMe');
    await page.locator('#storage-tab-panel form button[type="submit"]').click();
    await expect(page.getByText('Storage settings saved.')).toBeVisible();

    // Saving creates the config row, so the Connect control becomes available without a reload.
    // (Actually completing the Google OAuth redirect stays out of E2E, per the #23 decision;
    // the callback is covered by the fake-client pytest in tests/test_storage_google_drive.py.)
    await expect(connectButton).toBeVisible();
  });

  test('folder path round-trips through a save and reload', async ({ page }) => {
    await registerNewUser(page, 'storage-persist');
    await page.goto('/settings');

    const folder = '/OrganizeMe/exports';
    await page.locator('#folder_path').fill(folder);
    await page.locator('#storage-tab-panel form button[type="submit"]').click();
    await expect(page.getByText('Storage settings saved.')).toBeVisible();

    // Reload from the server to prove the value was persisted, not just held in client state.
    await page.reload();
    await expect(page.locator('#folder_path')).toHaveValue(folder);
  });
});
