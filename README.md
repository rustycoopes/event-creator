# Event Creator

Independent Cloud Run service for the OrganizeMe platform restructure — the second hosted app
behind the shared Load Balancer at `organizeme.qa.russcoopersoftware.com` (QA) /
`organizeme.russcoopersoftware.com` (prod). See the platform-restructure WBS in the
[`organize-me`](https://github.com/rustycoopes/organize-me) repo, specifically
`docs/platform-restructure/WBS/slice-R6.md`.

## What this is (Slice R6)

The tracer bullet proving the Host↔Event Creator boundary end-to-end: `GET /dashboard` verifies
the Host-issued JWT (signature + expiry only, via the shared `organizeme-chrome` package's
`jwt_verify` helper), extracts the user id, and renders a full page — shared chrome + a
placeholder Dashboard body — with **no** call back to the Host and **no** login/session code of
its own. Real dashboard content lands in a later parity slice (R9).

## Architecture

- **Trust, don't verify login**: `app/core/auth.py` only answers "which user is this" from the
  `organizeme_auth` cookie's JWT. No password handling, no session store, no network call to the
  Host.
- **Independent deploy**: its own repo, CI/CD, and Cloud Run service (`event-creator-qa` /
  `event-creator-prod`) — a push to this repo's `main` never touches the Host.
- **Shared schema, independent Alembic history**: owns the `event_creator` Postgres schema (tables
  moved there by the Host's own R1 migration) with its own version table
  (`event_creator.alembic_version`, see `migrations/env.py`), connecting to the same shared
  Postgres/Supabase instance as the Host. `migrations/versions/0001_adopt_event_creator_schema.py`
  is a no-op baseline — the tables already exist, this just adopts them into this repo's history.
- **Shared chrome**: consumes the `organizeme-chrome` package (pinned git tag) from the
  `organize-me` repo for the sidebar/theme/JWT-verify helper, same as the Host.

## Running locally

```bash
cp .env.local.example .env.local   # fill in DATABASE_URL + JWT_SECRET (same value as the Host's)
uv sync --group dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

## Testing

```bash
uv run pytest
uv run mypy app tests
```

## Deploying

CI/CD mirrors the Host's pattern (`.github/workflows/ci.yml` for QA on PRs,
`deploy.yml` for prod on push to `main`). Both read `JWT_SECRET` via
`--set-secrets` from the same GCP Secret Manager secrets the Host uses
(`jwt-secret-qa` / `jwt-secret-prod`) — no network call between services, just the same signing
key. Required GitHub secrets on this repo: `GCP_SA_KEY`, `SUPABASE_QA_URL`, `SUPABASE_PROD_URL`,
`JWT_SECRET_QA`, `JWT_SECRET_PROD` (the latter two only used by the `test` job's local
pytest/alembic run, not the deploy itself).

After the first deploy, re-run `organize-me`'s `infra/gcp_lb/provision.sh` (or `.ps1`) to attach
this service's NEG/backend to the shared Load Balancer's URL map.

## Deployed services

- Prod: `event-creator-prod` (Cloud Run, `northamerica-northeast1`) — first deployed 2026-07-12.
- QA: `event-creator-qa` — deploys on every PR into `main` via `ci.yml`.
