import { test, expect } from '@playwright/test';
import { registerNewUser, uploadFile } from '../utils/helpers';

/**
 * Slice R11 (#166) — the Upload page had no dedicated e2e coverage of its own (only as a means to
 * an end inside processing.spec.ts, which drives it purely to get a run started). Added while
 * closing the R11 verification gap for PRD stories 24-25. Deliberately doesn't re-assert the
 * upload -> successful-pipeline-run happy path end to end (a full SSE wait to a terminal state) -
 * that's processing.spec.ts's job; this file covers what's unique to the Upload page itself: the
 * client-rejected-file feedback, plus the storage-status banner's actual observable behaviour.
 */
test.describe('Upload page', () => {
  test('uploads proceed for a user with no storage connected (ephemeral fallback)', async ({
    page,
  }) => {
    await registerNewUser(page, 'upload-ephemeral');
    await page.goto('/upload');

    // E2E_TEST_MODE forces is_storage_connected() to return true for every user regardless of
    // real connection state (app/api/v1/storage_config.py's e2e_test_mode short-circuit, mirrored
    // from the Host's own is_drive_connected), so the ephemeral-fallback warning banner is never
    // shown on QA - assert its actual (absent) state rather than the unobservable disconnected
    // case, and instead prove the real thing issue #79 guarantees: uploads are never blocked on
    // storage being connected.
    await expect(page.getByText('No storage provider is connected.')).not.toBeVisible();

    await uploadFile(page, 'ephemeral-chat.txt', 'E2E upload ephemeral-fallback test.\n');
  });

  test('rejects an unsupported file type with an inline error, without leaving the page', async ({
    page,
  }) => {
    await registerNewUser(page, 'upload-unsupported');
    await page.goto('/upload');

    // The picker's `accept` attribute only narrows the OS file dialog - setInputFiles bypasses
    // it, exercising the server-side extension check (app.api.v1.upload) the same way a drag-
    // and-dropped file with the wrong extension would.
    await page.locator('#file-input').setInputFiles({
      name: 'not-a-conversation.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4 fake pdf content'),
    });

    await expect(page.getByText('Please upload a .txt, .zip, or .csv file.')).toBeVisible();
    await expect(page).toHaveURL(/\/upload$/);
  });

  test('rejects an empty file with an inline error', async ({ page }) => {
    await registerNewUser(page, 'upload-empty');
    await page.goto('/upload');

    await page.locator('#file-input').setInputFiles({
      name: 'empty.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(''),
    });

    await expect(page.getByText('That file is empty.')).toBeVisible();
    await expect(page).toHaveURL(/\/upload$/);
  });
});
