import { test, expect } from '@playwright/test';
import { registerNewUser } from '../utils/helpers';

test.describe('Prompt page', () => {
  test('edit -> save -> reload persists, then reset restores the default', async ({ page }) => {
    await registerNewUser(page, 'prompt');
    await page.goto('/prompt');

    const textarea = page.locator('#prompt_text');
    // A brand-new account is seeded the factory default (#48), so the editor opens with it.
    await expect(textarea).toContainText(
      'extracts agreed plans and commitments from a WhatsApp conversation',
    );

    // Edit and save.
    const edited = 'Only extract dentist appointments and swimming lessons.';
    await textarea.fill(edited);
    await page.locator('form button[type="submit"]').click();
    await expect(page.getByText('Prompt saved.')).toBeVisible();

    // Reload from the server to prove the edit was persisted, not just held in client state.
    await page.reload();
    await expect(page.locator('#prompt_text')).toHaveValue(edited);

    // Reset to default restores the factory prompt without a reload.
    await page.locator('#reset-prompt').click();
    await expect(page.getByText('Prompt reset to default.')).toBeVisible();
    await expect(page.locator('#prompt_text')).toHaveValue(
      /extracts agreed plans and commitments from a WhatsApp conversation/,
    );
    await expect(page.locator('#prompt_text')).not.toHaveValue(edited);

    // ...and the reset survives a reload too.
    await page.reload();
    await expect(page.locator('#prompt_text')).toHaveValue(
      /extracts agreed plans and commitments from a WhatsApp conversation/,
    );
  });
});
