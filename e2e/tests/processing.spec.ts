import { test, expect } from '@playwright/test';
import { registerNewUser, uploadFile } from '../utils/helpers';

/**
 * Slice 4.2 (#53) — live pipeline progress.
 *
 * Drives a real upload through the deployed QA app and asserts the 7 step indicators advance to a
 * terminal state live via the SSE stream. Runs under E2E_TEST_MODE, where (per #52/#53's testability
 * seam) the storage provider resolves to the in-memory fake and the Gemini extract step to a canned
 * fake — so the upload -> pipeline -> SSE flow runs to a *successful* terminal state without a real
 * Google Drive connection or a GEMINI_API_KEY.
 */
test.describe('Processing progress page', () => {
  test('upload drives the 7 step indicators live to a successful terminal state', async ({
    page,
  }) => {
    await registerNewUser(page, 'processing');

    // Upload a small conversation. Under E2E_TEST_MODE the dropzone is enabled without a real
    // Drive connection, and the hidden file input feeds the same submit path as drag-and-drop.
    // The upload page follows the run to the live progress page.
    await uploadFile(page, 'chat.txt', 'E2E processing test conversation.\n');

    // The 7 step indicators render...
    for (let n = 1; n <= 7; n++) {
      await expect(page.locator(`#step-${n}`)).toBeVisible();
    }

    // ...and the run reaches a successful terminal state live (via SSE, no manual refresh).
    await expect(page.locator('[data-run-status="success"]')).toBeVisible({ timeout: 45_000 });

    // Every step finished: none left pending or in-progress. Extract is skipped for a .txt.
    await expect(page.locator('#step-1 [data-status="success"]')).toBeVisible();
    await expect(page.locator('#step-2 [data-status="skipped"]')).toBeVisible();
    await expect(page.locator('#step-7 [data-status="success"]')).toBeVisible();
    await expect(page.locator('[data-status="pending"]')).toHaveCount(0);
    await expect(page.locator('[data-status="in_progress"]')).toHaveCount(0);
  });
});
