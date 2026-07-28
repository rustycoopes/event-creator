# Slice 2 — Bulk mark as reviewed

> Part of the `dashboard-bulk-actions` feature. PRD: [`../PRD.md`](../PRD.md) · Technical design:
> [`../TDD.md`](../TDD.md)

**Delivers:** A user can select multiple events on the Dashboard and mark them all as reviewed in
one action, with no confirmation dialog (mirroring the existing single-row Reviewed toggle's
no-confirmation behavior).

## What to build

**Frontend** (additive to Slice 1's toolbar and Alpine scope — no new selection UI):
- A second toolbar button, "Mark Selected as Reviewed", using the `primary` (cobalt) button
  variant — unused elsewhere on this page, giving it clear visual weight distinct from Import
  (amber) and Delete Selected (red).
- Applies immediately on click, no confirmation dialog — one-directional, always sets
  `reviewed=true` on the selected events. There is no bulk "unmark reviewed" action; un-reviewing
  stays a per-row action via the existing toggle.
- Same error/refresh wiring as Slice 1's bulk delete: failures write into the shared `error`
  field and render via the existing inline `alert` component ("N of M events marked as
  reviewed"); success re-fetches `#dashboard-body` via the existing HTMX pattern, which also
  resets selection.

**Backend** (`app/api/v1/events.py`, reusing Slice 1's `BulkEventIdsRequest`/`BulkActionResult`
schemas as-is — no schema changes needed):
- `POST /api/v1/events/bulk-review`: a single `UPDATE ... SET reviewed = true WHERE id IN (...)
  AND user_id = :user_id RETURNING id` statement, diffed the same way Slice 1's bulk-delete is.
  The `WHERE` is *not* additionally scoped to `reviewed IS FALSE` — an already-reviewed event
  submitted again still matches and lands in `succeeded_ids` (a no-op at the storage level), so a
  mixed reviewed/unreviewed selection never produces a confusing `failed_id` for a row that was
  already in the target state.
- Same per-user ownership scoping and best-effort semantics as `bulk-delete`: an ID that doesn't
  exist or belongs to another user lands in `failed_ids`, never a 403, never a request-level 404.
- One structured audit log line per call, same shape as Slice 1's.

## Design notes

- Idempotency on already-reviewed events and the "no `WHERE reviewed IS FALSE`" reasoning: TDD §
  Best-effort semantics, implemented as a single `RETURNING` statement.
- `extra="forbid"` on `BulkEventIdsRequest` (already shipped in Slice 1) is what makes
  bulk-unreview structurally unreachable through this endpoint — a request body carrying a
  `reviewed` field 422s rather than silently being accepted and ignored. No new validation needed
  in this slice, just confirm the existing schema is reused unchanged.
- No service layer, same rationale as Slice 1:
  [`docs/adr/dashboard-bulk-actions-no-service-layer.md`](../../../adr/dashboard-bulk-actions-no-service-layer.md).

## Blocked by

- [Slice 1](slice-1-bulk-delete.md) — reuses its selection UI, toolbar shell, Alpine scope
  extension, and shared request/response schemas. This slice adds nothing to the selection
  mechanism itself.

## Acceptance criteria

- [ ] "Mark Selected as Reviewed" is visible in the toolbar once ≥1 row is selected, styled
      distinctly (primary/cobalt) from Delete Selected (red) and Import (amber).
- [ ] Clicking it applies immediately with no confirmation dialog.
- [ ] Marking selected events as reviewed removes them from view once applied (since "Show
      reviewed" defaults off), matching the existing single-row toggle's behavior, and the table
      refreshes in place.
- [ ] A selection mixing already-reviewed and not-yet-reviewed events succeeds for all of them —
      an already-reviewed event is never reported in `failed_ids`.
- [ ] A bulk-review request mixing the caller's own event IDs with another user's (or nonexistent)
      IDs updates only the caller's own events and reports the others in `failed_ids`.
- [ ] `POST /api/v1/events/bulk-review` requires auth (401), rejects an empty list and a list over
      50 items (422), and rejects a request body containing a `reviewed` field (422, proving
      bulk-unreview is unreachable).
- [ ] Selection clears after the bulk action completes.

## Testing

**Primary seam — `e2e/tests/dashboard.spec.ts`** (extends Slice 1's new specs in the same file):
bulk mark-reviewed applies with no confirm dialog, marked rows disappear from the default
("Show reviewed" off) view, button styling is distinct from Delete Selected.

**Secondary seam — `tests/test_events_api.py`** (extends Slice 1's new cases, reusing its
`_make_events` helper): auth-required, empty-list/over-page-size/extra-field 422s, idempotency on
already-reviewed events, and the same "mixed owned/unowned IDs" assertion-on-response-body pattern
Slice 1 established for `bulk-delete`, applied to `bulk-review`.

<!-- /to-implementation appends a "## Delivered" section here once this slice ships. -->

## Delivered (2026-07-28, issue #42, branch `feature/dashboard-bulk-mark-reviewed`)

Shipped as planned: `POST /api/v1/events/bulk-review` (single `RETURNING`-based `UPDATE ... SET
reviewed = true WHERE id IN (...) AND user_id = :user_id`, deliberately not scoped to `WHERE
reviewed IS FALSE`), reusing Slice 1's `BulkEventIdsRequest`/`BulkActionResult` schemas unchanged,
and a "Mark Selected as Reviewed" (`primary`/cobalt) toolbar button in `events_panel.html` that
applies immediately with no confirmation dialog.

One thing generalized beyond the plan: the existing `bulkDeleteFlash` sessionStorage key (Slice 1's
mechanism for surviving the HTMX swap that destroys the Alpine scope before a partial-failure
message can render) was renamed to `bulkActionFlash` and is now shared by both bulk-delete and
bulk-review, rather than adding a second, near-identical `bulkReviewFlash` key. Verified no other
code referenced the old key name before renaming.

`code-review-master` and `code-quality-guardian` both reviewed the diff and converged on the same
single (non-blocking) finding: `BulkEventIdsRequest`'s docstring in `app/schemas/event.py` still
said "currently just bulk-delete", stale now that bulk-review reuses the same schema. Fixed as part
of this slice. No other correctness, security, or simplification issues found.

Backend pytest suite (38 tests, including 7 new `test_bulk_review_*` cases) and `mypy` both run
clean locally against the QA Supabase database. The e2e spec (`dashboard.spec.ts`) was not run
locally — this repo's Playwright suite targets the deployed QA app via the shared Load Balancer
domain, not localhost, so it's verified through CI's `deploy-qa` → `e2e-qa` pipeline on the PR
instead, per this repo's normal flow.

A real bug slipped past both static code review and local `mypy`/pytest, and was only caught by
CI's `e2e-qa` run against the deployed QA app: the JS comment "matches the existing per-row
Reviewed toggle's own no-confirmation behavior" inside `bulkMarkReviewed()` contained an apostrophe,
which prematurely closed the single-quoted `x-data='...'` HTML attribute. The remaining JS source
leaked out as literal page text and corrupted the entire events table's rendering — not just the
new button, every existing `dashboard.spec.ts` test failed the same run (5 failed, all timing out
or failing to find elements, including pre-existing single-row delete/select-all specs untouched by
this diff). Fixed by rewording the comment to avoid the apostrophe, verified by rendering the
template directly with Jinja outside the test suite (confirmed the toolbar `</div>` now closes
cleanly before `<table>`), then re-pushed for CI to re-verify.
