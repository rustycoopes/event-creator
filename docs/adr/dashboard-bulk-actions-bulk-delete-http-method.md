# Bulk delete uses POST to an action path, not DELETE with a request body

**Status:** Proposed
**Date:** 2026-07-27
**Feature:** [`dashboard-bulk-actions`](../features/dashboard-bulk-actions/TDD.md)

## Context

The existing single-event endpoint is `DELETE /api/v1/events/{event_id}`, returning `204 No
Content`. Bulk delete needs to accept a list of event IDs and needs to return a body
(`succeeded_ids`/`failed_ids`, since processing is best-effort) — it can't stay a clean `204`.

`DELETE` requests with a JSON body are valid HTTP, and in this specific stack (same-origin
`fetch`/HTMX calls, behind Caddy in local dev and Cloud Run's edge in prod) there's no evidence
either would strip the body — so "would `DELETE` with a body actually break here" is not, on its
own, a strong argument either way.

## Decision

Use `POST /api/v1/events/bulk-delete` (and, for symmetry, `POST /api/v1/events/bulk-review` for
the other bulk endpoint), both returning `200` with a `BulkActionResult` body.

## Alternatives considered

**`DELETE /api/v1/events` with a JSON body listing the IDs.** Rejected on two grounds independent
of whether the transport would technically work:

- **Semantic mismatch.** `DELETE` returning `204` is the correct shape for "this resource is gone,
  nothing more to say." Once bulk delete needs to return a body reporting partial success, it has
  already given up the main reason to model it as a REST `DELETE` on a resource — it's an
  RPC-style action ("perform this bulk operation and tell me what happened"), not idempotent
  single-resource removal. A `POST` to an explicit action path (`bulk-delete`) names that honestly.
- **Client/tooling friction.** `httpx.AsyncClient.delete()` (used throughout this repo's existing
  test suite) doesn't accept a `json=` body in the same ergonomic way `.post()` does — every test
  written against a `DELETE`-with-body endpoint pays a small, recurring tax (`.request("DELETE",
  ..., json=...)` instead of `.delete(..., json=...)`) for a shape that gains nothing over `POST`
  once the "REST purity" argument is already gone.

## Consequences

- Both bulk endpoints are unambiguously RPC-style actions, not REST resource operations — this
  sets the pattern any future bulk endpoint on this resource (e.g. a hypothetical "bulk change
  type") should follow: `POST /api/v1/events/bulk-<verb>`, not an overloaded `DELETE`/`PATCH` on
  the collection.
- `bulk-delete` and `bulk-review` both return `200 BulkActionResult` uniformly, rather than one
  endpoint being `204` and the other `200` — simpler for the frontend to handle generically.
- Loses `DELETE`'s implicit "this is safe to retry/idempotent by HTTP semantics" signal at the
  transport level; idempotency here is instead a property of the implementation (re-submitting the
  same IDs is safe because a `RETURNING`-based `DELETE ... WHERE id IN (...)` naturally no-ops on
  already-deleted rows), not something an HTTP client or proxy can assume from the method alone.
