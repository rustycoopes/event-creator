import { test, expect } from '@playwright/test';
import { registerNewUser } from '../utils/helpers';

test.describe('Settings > Notifications', () => {
  test('SMS toggle is disabled until a phone number is on file, then persists once set', async ({
    page,
  }) => {
    await registerNewUser(page, 'notif-sms');
    await page.goto('/settings');
    await page.getByRole('tab', { name: 'Notifications' }).click();

    // Fresh account: email is set (from registration) but phone is not, so SMS starts disabled.
    await expect(page.locator('#notification_email')).toBeEnabled();
    await expect(page.locator('#notification_sms')).toBeDisabled();
    await expect(page.getByText('Set your phone number in Profile to enable.')).toBeVisible();

    // Set a phone number via Profile, then return to Settings > Notifications.
    await page.goto('/profile');
    await page.locator('#phone_number').fill('+15551234567');
    await page.locator('form button[type="submit"]').click();
    await expect(page.getByText('Changes saved.')).toBeVisible();

    await page.goto('/settings');
    await page.getByRole('tab', { name: 'Notifications' }).click();
    await expect(page.locator('#notification_sms')).toBeEnabled();

    await page.locator('#notification_sms').uncheck();
    await page.locator('#notifications-tab-panel form button[type="submit"]').click();
    await expect(page.getByText('Notification preferences saved.')).toBeVisible();

    // Reload from the server to prove the toggle was persisted, not just held client-side.
    await page.reload();
    await page.getByRole('tab', { name: 'Notifications' }).click();
    await expect(page.locator('#notification_sms')).not.toBeChecked();
  });

  test('email toggle round-trips through a save and reload', async ({ page }) => {
    await registerNewUser(page, 'notif-email');
    await page.goto('/settings');
    await page.getByRole('tab', { name: 'Notifications' }).click();

    // Both channels default on; toggling email off and saving persists across a reload.
    await expect(page.locator('#notification_email')).toBeChecked();
    await page.locator('#notification_email').uncheck();
    await page.locator('#notifications-tab-panel form button[type="submit"]').click();
    await expect(page.getByText('Notification preferences saved.')).toBeVisible();

    await page.reload();
    await page.getByRole('tab', { name: 'Notifications' }).click();
    await expect(page.locator('#notification_email')).not.toBeChecked();
  });
});
