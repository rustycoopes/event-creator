# Bulk endpoints stay inline in the router — no service/repository layer

**Status:** Proposed
**Date:** 2026-07-27
**Feature:** [`dashboard-bulk-actions`](../features/dashboard-bulk-actions/TDD.md)

## Context

`app/api/v1/events.py` currently has no layer between FastAPI route handlers and SQLAlchemy: each
handler calls a plain async query function (`get_owned_event`, `list_user_events`) directly and
commits its own transaction. This is true of every feature in this codebase, not just events —
there is no service or repository layer anywhere in the app.

Bulk delete and bulk mark-reviewed are more consequential single actions (up to 50 rows at once)
than the single-event operations they sit next to, which raises the question of whether they
deserve their own encapsulating abstraction — e.g. a `BulkEventService` with named methods like
`delete_selected()` — rather than being two more router-level functions.

## Decision

Add both bulk endpoints as plain functions directly in `app/api/v1/events.py`, in the same style as
the existing single-event handlers: dependency-injected `user_id`/`db`, one query, one commit, no
intermediate object.

## Alternatives considered

**A `BulkEventService` (or similar) encapsulating the two bulk operations.** Rejected:

- It would create an asymmetry with no real justification: single-event delete/update stay inline
  in the router (query → mutate → commit, a handful of lines), while bulk delete/update alone move
  behind a service class — on the same resource, in the same file, reachable by the same user. A
  future reader has no honest answer for *why* bulk gets an object and single doesn't beyond "it
  was built later, by someone who defaulted to more structure."
- There's no complexity here that actually needs encapsulating. Each bulk operation is one
  `RETURNING`-based statement plus a diff against the request — a service class wouldn't hide any
  real complexity, it would wrap four lines in ceremony.
- This codebase's existing convention (routers call plain query functions directly, everywhere) is
  a real design constraint, not an oversight to correct opportunistically. Introducing the first
  service-layer object anywhere in the app, scoped narrowly to two endpoints, would be a bigger
  architectural precedent than this feature warrants setting unilaterally.

## Consequences

- `app/api/v1/events.py` grows by roughly two more route handlers (~40-60 lines) — not yet a
  file-size problem, but worth watching if more bulk/event operations accumulate here.
- Keeps a single, consistent mental model for this resource: every operation (single or bulk) is a
  thin router function over a query function, with ownership scoping folded into the query's
  `WHERE` clause. No reader has to learn two different access patterns for the same resource.
- If a *future* bulk operation needs genuine multi-table transactional coordination or cross-cutting
  concerns this codebase doesn't have a home for yet (e.g. "bulk export and email", spanning
  notifications and storage), that's the point to introduce a shared orchestration seam — this
  decision doesn't foreclose that, it just declines to build it preemptively for two straightforward
  mutations that don't need it.
