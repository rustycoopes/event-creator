## Problem Statement

On the Dashboard, every review action (marking an event reviewed, deleting an event) is scoped to
one row at a time. A user with several events to clean up in one sitting — e.g. after importing a
batch of pending files — has to repeat the same click sequence once per event, with a full delete
confirmation dialog on every single deletion. There's no way to act on more than one event per
interaction.

Separately, two controls already exist on the Dashboard but are effectively invisible in
production: the "Import pending files" button (rendered as unstyled text, no button chrome) and
the per-row Reviewed toggle (rendered with no visible track/thumb). Investigation traced this to a
stale/incomplete compiled CSS bundle, not a missing feature — that's being fixed separately as its
own bug, outside this PRD.

## Solution

Add multi-select to the Dashboard's events table: a checkbox on every row plus a "select all"
checkbox in the table header that selects/deselects every row on the current page. Selecting one
or more rows reveals a bulk-action toolbar with two actions — "Delete Selected" and "Mark Selected
as Reviewed" — that operate on every selected event in one request instead of one-by-one.

## User Stories

1. As a user with several events to clean up, I want to select multiple events at once, so that I
   don't have to repeat the same action once per event.
2. As a user, I want a checkbox on each event row, so that I can pick exactly which events to act
   on.
3. As a user, I want a "select all" checkbox in the table header, so that I can select every event
   currently visible in one click instead of checking each row individually.
4. As a user, I want the "select all" checkbox to only affect the current page of results, so that
   I don't accidentally select events I can't currently see (e.g. under a filter or on another
   page) that I never reviewed the context of.
5. As a user, I want a bulk-action toolbar to appear only once I've selected at least one event, so
   that the page stays uncluttered when I'm not doing a bulk action.
6. As a user, I want to see how many events are currently selected, so that I have a clear
   confirmation of the scope of the action I'm about to take.
7. As a user, I want a "Delete Selected" button, so that I can remove multiple events in one action.
8. As a user, I want a confirmation dialog before a bulk delete happens, so that I don't
   accidentally destroy several events at once with one misclick.
9. As a user, I want the confirmation dialog to state how many events will be deleted, so that I
   know the exact scope of the destructive action before confirming.
10. As a user, I want a "Mark Selected as Reviewed" button, so that I can clear multiple events out
    of my unreviewed view in one action.
11. As a user, I want "Mark Selected as Reviewed" to apply immediately with no confirmation dialog,
    so that a low-risk, reversible action doesn't require an extra click (consistent with the
    existing single-row Reviewed toggle, which also requires no confirmation).
12. As a user, I want the events table to update in place (no full page reload) after a bulk action
    completes, so that my scroll position, filters, and search text aren't lost.
13. As a user, I want a bulk delete or bulk mark-reviewed to still complete successfully for the
    events that are valid, even if one selected event turns out to be stale (e.g. deleted from
    another tab moments earlier), so that one edge case doesn't block the rest of my selection.
14. As a user, I want to be told if part of a bulk action failed (e.g. "4 of 5 events deleted"), so
    that I know my data ended up in the state I expect.
15. As a user, I want my selection to clear automatically whenever I change a filter, change sort
    order, change page, or complete a bulk action, so that I never accidentally act on events that
    are no longer the ones I'm looking at.
16. As a user, I want the "select all" header checkbox to reflect an indeterminate state when only
    some (not all) rows on the page are selected, so that its visual state always matches reality.
17. As a developer, I want bulk delete and bulk mark-reviewed to each be a single backend request
    carrying a list of event IDs, so that the operation is one round trip instead of one request
    per event.
18. As a developer, I want the bulk endpoints to enforce the same per-user ownership scoping as the
    existing single-event endpoints, so that a user can never affect another user's events even by
    constructing a bulk request by hand.
19. As a user, I want both bulk actions to only ever affect events I own, so that a malformed or
    tampered request can't touch anyone else's data.

## Implementation Decisions

**Scope:** `event-creator` repo only. No changes to `organize-me` (the Host) or any other hosted
app.

**Selection UI** (Dashboard events table, `events_panel.html` + its Alpine `x-data` scope):
- A new leftmost table column holds a checkbox per row, reusing the same visual/markup pattern as
  the existing Reviewed toggle's underlying `<input type="checkbox">` where practical, but as a
  plain (non-switch) checkbox — this is a selection control, not a boolean-state control, so it
  should read visually as a checkbox, not the track+thumb toggle used for Reviewed.
- The table header's first cell holds a "select all" checkbox. Checking it selects every row
  currently rendered in the table body (i.e. the current page, current filter/sort); unchecking it
  clears all selections. It reflects three states: unchecked (nothing selected), checked (every row
  on the page selected), indeterminate (some but not all rows selected).
- Selection state lives in the existing Alpine `x-data` scope already used for the delete-confirm
  dialog and the Reviewed-toggle's `toggleReviewed()` method — no new state-management approach
  introduced.
- Selection resets to empty on: any HTMX-driven table refresh (filter change, sort change, page
  change, a bulk action completing) and on a single-row delete completing. It does not persist
  across these events.

**Bulk-action toolbar:**
- Hidden by default (`x-show`-gated on "at least one row selected"); appears above the table
  (between the "N events total" count line and the table itself) once ≥1 row is checked.
- Shows a live count ("N selected") plus two buttons: "Delete Selected" and "Mark Selected as
  Reviewed".
- "Delete Selected" reuses the `danger-solid` button variant (same as the existing per-row Delete
  button) and gates on a confirmation dialog, extending the existing hand-rolled `<dialog>`
  confirm-delete pattern to show a count ("Delete N events? This cannot be undone.") instead of a
  single description.
- "Mark Selected as Reviewed" uses the `primary` button variant (cobalt) — defined in the shared
  `organizeme_chrome` button component but currently unused anywhere on this page, giving it clear
  visual weight distinct from Import (amber/`secondary`) and Delete (red/`danger-solid`). It applies
  immediately on click with no confirmation dialog, mirroring the existing single-row Reviewed
  toggle's no-confirmation behavior. This is one-directional: it only ever sets `reviewed=true` on
  selected events. Un-marking stays a per-row action via the existing toggle; no bulk "unmark"
  action is introduced.

**Backend — two new bulk endpoints**, mirroring the existing single-event `DELETE`/`PATCH`
endpoints' auth and ownership-scoping behavior exactly (requester's `user_id`, 401 if
unauthenticated, never confirms existence of another user's event to the caller):
- A bulk delete endpoint accepting a list of event IDs in the request body, removing every event in
  the list that belongs to the requesting user.
- A bulk reviewed-update endpoint accepting a list of event IDs in the request body, setting
  `reviewed=true` on every event in the list that belongs to the requesting user. (No bulk
  "unreviewed" variant, per the one-directional decision above.)
- Both endpoints process on a best-effort basis: IDs that don't exist, or belong to another user,
  are skipped rather than failing the whole request. The response reports how many succeeded and
  which submitted IDs (if any) were not found/not owned by the caller, so the frontend can render a
  "N of M succeeded" message when the two counts differ.
- Both endpoints reuse the existing `get_owned_event`-style ownership-scoping query per ID (or an
  equivalent set-based query scoped to `user_id`) rather than introducing a new authorization
  mechanism.

**Frontend wiring:**
- On success, the bulk-action's response drives an HTMX re-fetch of the same `#dashboard-body`
  swap target the filter form and the single-row Reviewed toggle already use, so the table, event
  count, and pagination all stay consistent with the new state in one refresh — no full page
  reload.
- On a partial-failure response, render the existing inline `alert` component ("N of M events
  deleted" / "N of M events marked as reviewed") rather than introducing a new error-surfacing
  pattern, consistent with how import and single-event delete failures already surface.

## Testing Decisions

A good test here exercises the feature the way a user (Playwright) or another service (pytest/
httpx) would — asserting on visible page state and response bodies, not on internal Alpine state or
SQL query shape.

**Primary seam — `e2e/tests/dashboard.spec.ts` (Playwright, existing file):** this file already
drives the full stack (real HTTP, real DB, real browser) against the Dashboard, including a
`'delete removes an event behind a confirm dialog'` test whose structure — upload two canned
events via `E2E_TEST_MODE`'s fake extraction payload, then interact with the table — is the direct
template for the new specs:
- Selecting individual rows and the header checkbox reveals the toolbar with the correct count and
  toggles the header checkbox's indeterminate state correctly.
- "Delete Selected" is gated behind a confirmation dialog stating the correct count, and removes
  exactly the selected events (not others) once confirmed, matching the existing single-delete
  test's structure of asserting on visibility before and after.
- "Mark Selected as Reviewed" applies with no confirmation dialog, and the marked events drop out
  of view (since "Show reviewed" defaults off), matching the existing reviewed-toggle behavior.
- Selection clears after a filter change, a sort change, and after a bulk action completes.

**Secondary seam — `tests/test_events_api.py` (pytest + httpx, existing file):** covers backend
edge cases impractical to set up through a real browser (would require multi-user fixtures per
test case, which this file's existing tests — e.g.
`test_delete_returns_404_for_another_users_event` — already do easily via
`tests/conftest.py`'s `TokenFactory`/`create_host_user`):
- Both bulk endpoints require auth (401 unauthenticated), mirroring
  `test_delete_requires_auth`/`test_patch_requires_auth`.
- A bulk request mixing IDs owned by the requester with IDs owned by another user only affects the
  requester's own events, and reports the other user's IDs as not-found/skipped — never a 403 or
  error that would confirm those IDs' existence to the caller (matching the existing single-event
  endpoints' "404, not 403" ownership-scoping behavior).
- A bulk request mixing valid IDs with nonexistent IDs succeeds for the valid ones and reports the
  nonexistent ones, rather than failing the whole batch.
- Bulk reviewed-update only ever sets `reviewed=true`, never toggles or clears it, even if some
  selected events are already reviewed (idempotent no-op for those).

## Out of Scope

- Selecting events beyond the current page (e.g. "select all N events matching this filter across
  every page") — explicitly deferred; select-all is scoped to the current page only.
- A bulk "mark as unreviewed" action — un-reviewing stays a per-row action via the existing toggle.
- Any change to the single-row Delete or single-row Reviewed-toggle behavior — both are left as-is.
- The stale/incomplete compiled CSS bug affecting the Import button and Reviewed toggle's current
  visibility in production — tracked and fixed separately, not part of this feature.
- Any change to `organize-me` (the Host), `doc-library`, or `ha-dashboard`.
- A maximum-selection-count guard beyond the natural page-size cap (50 events per page).

## Further Notes

- This is the first PRD written under `event-creator/docs/features/` — the repo has no
  `docs/features/` directory or `CLAUDE.md` yet. Neither is created as part of this PRD; only this
  feature's directory.
- The existing single-event Delete and Reviewed-toggle flows (`events_panel.html`,
  `app/api/v1/events.py`) are the direct precedent for nearly every decision above — this feature
  is additive on top of fully-working existing plumbing, not new ground.
- `/to-design` should confirm the exact bulk-endpoint request/response schema shapes (field names,
  status codes) and the precise Alpine/HTMX wiring for indeterminate-checkbox state, since those
  are implementation-precise details better pinned down with a design pass than guessed here.
