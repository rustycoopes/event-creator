# Slice 1 — Bulk delete

> Part of the `dashboard-bulk-actions` feature. PRD: [`../PRD.md`](../PRD.md) · Technical design:
> [`../TDD.md`](../TDD.md)

**Delivers:** A user can select multiple events on the Dashboard and delete them all in one action,
behind a single confirmation dialog stating how many will be deleted.

## What to build

**Selection UI** (`events_panel.html`'s events table, extending its existing Alpine `x-data`
scope):
- A new leftmost checkbox column on every event row.
- A "select all" checkbox in the table header, selecting/deselecting every row currently rendered
  (i.e. the current page/filter/sort — never events outside what's visibly rendered). Reflects
  three states: unchecked, checked (every visible row selected), indeterminate (some but not all).
- A bulk-action toolbar, hidden until at least one row is checked, showing a live "N selected"
  count. This slice ships it with one button: "Delete Selected".
- Selection resets to empty on any table refresh — filter change, sort change, page change, a
  bulk action completing, or a single-row delete completing. This falls out for free from the
  existing HTMX re-fetch-and-swap pattern (the whole fragment, including the `x-data` scope,
  gets replaced) — no separate reset logic to write, just don't fight it.
- "Delete Selected" reuses the `danger-solid` button variant (same as the existing per-row Delete
  button) and opens a confirmation dialog extending the existing hand-rolled `<dialog>`
  confirm-delete pattern, showing the selected count ("Delete N events? This cannot be undone.").
- Bulk-action errors write into the same `error` Alpine field the existing delete/toggle handlers
  already use — not a new field. On a partial-failure response (`failed_ids` non-empty), render
  the existing inline `alert` component with a "N of M events deleted" message.
- On success, re-fetch `#dashboard-body` via the same HTMX pattern `toggleReviewed()` already uses
  — no full page reload.

**Backend** (`app/api/v1/events.py`, alongside the existing single-event handlers — no new
module, no service layer):
- `BulkEventIdsRequest` and `BulkActionResult` Pydantic schemas (`app/schemas/event.py`), shared
  by this endpoint and Slice 2's.
- `POST /api/v1/events/bulk-delete`: a single `DELETE ... WHERE id IN (...) AND user_id = :user_id
  RETURNING id` statement, diffed against the (de-duplicated) requested IDs to build
  `succeeded_ids`/`failed_ids`. Same ownership-scoping guarantee as the existing single-event
  `DELETE` — an ID that doesn't exist or belongs to another user lands in `failed_ids`, never a
  403, never a request-level 404.
- One structured audit log line per call (actor `user_id`, requested count, `succeeded_ids`,
  `failed_ids`).

## Design notes

- HTTP method/path choice (`POST`, not `DELETE` with a body) and its rationale:
  [`docs/adr/dashboard-bulk-actions-bulk-delete-http-method.md`](../../../adr/dashboard-bulk-actions-bulk-delete-http-method.md).
- No service/repository layer — endpoints stay inline in the router, matching every existing
  feature in this codebase:
  [`docs/adr/dashboard-bulk-actions-no-service-layer.md`](../../../adr/dashboard-bulk-actions-no-service-layer.md).
- Request validation (`min_length=1`, `max_length=PAGE_SIZE`, `extra="forbid"`) and the "never
  404 the whole request" rule: TDD § Request/response schemas.
- Both new paths must be true sub-paths of `/api/v1/events` (a literal `/` after `events`) for the
  existing wildcard proxy routing to pick them up with no registry/Caddyfile change — see TDD §
  HTTP method and path.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [ ] Checking one or more event rows reveals the bulk-action toolbar with an accurate live count.
- [ ] The header checkbox selects/deselects every row on the current page and shows the correct
      indeterminate state when only some rows are checked.
- [ ] "Delete Selected" is gated behind a confirmation dialog stating the exact number of events
      about to be deleted; nothing is deleted until confirmed.
- [ ] Confirming deletes exactly the selected events (not others) and the table refreshes in place
      (no full page reload).
- [ ] A bulk-delete request mixing the caller's own event IDs with another user's (or nonexistent)
      IDs deletes only the caller's own events and reports the others in `failed_ids` — never a
      403, never a request-level 404.
- [ ] `POST /api/v1/events/bulk-delete` requires auth (401 unauthenticated), rejects an empty
      `event_ids` list and a list over 50 items (422 both cases).
- [ ] Selection clears after a filter change, a sort change, a page change, and after a bulk
      delete completes.

## Testing

**Primary seam — `e2e/tests/dashboard.spec.ts`** (extends the existing file's
`'delete removes an event behind a confirm dialog'` pattern: register a user, upload the canned
two-event fixture, interact with the table): select-all/per-row checkbox behavior and indeterminate
state, toolbar visibility, bulk delete behind its confirm dialog with the correct count, selection
clearing on filter/sort change and after the bulk action completes.

**Secondary seam — `tests/test_events_api.py`** (extends the existing file, using
`tests/conftest.py`'s `TokenFactory`/`create_host_user`; add a small `_make_events(db, user_id,
run_id, count=2)` helper alongside the existing `_make_event`): auth-required, empty-list and
over-page-size 422s, and — the assertion shape that's new relative to the existing single-event
tests — a request mixing owned and unowned/nonexistent IDs asserted on `succeeded_ids`/
`failed_ids` *contents*, not just status code, since that's the actual regression guard for the
ownership-scoping guarantee.

<!-- /to-implementation appends a "## Delivered" section here once this slice ships. -->
