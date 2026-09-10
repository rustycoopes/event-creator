# Event Creator

Independent Cloud Run service for the OrganizeMe platform restructure — the second hosted app
behind the shared Load Balancer at `organizeme.russcoopersoftware.com` (prod). See the
platform-restructure WBS in the
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
- **Independent deploy**: its own repo, CI/CD, and Cloud Run service (`event-creator-prod`) — a
  push to this repo's `main` never touches the Host.
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

To run alongside the Host and other hosted apps behind a shared local proxy (real registry-synced
cross-app nav, one login for all apps, no Docker), use `organize-me`'s launcher instead — see its
["Local development"](https://github.com/rustycoopes/organize-me/blob/main/docs/local-development.md)
doc.

## Testing

```bash
uv run pytest
uv run mypy app tests
```

## Deploying

CI/CD: `.github/workflows/ci.yml` runs `pytest` + `mypy` on PRs; `deploy.yml` runs the same
suite and then deploys to prod on push to `main`. Both `test` jobs point `DATABASE_URL` at
Supabase prod (test writes roll back inside a SAVEPOINT) — event-creator's baseline migration is a
no-op that adopts organize-me-created tables, so there's no schema to stand up on a throwaway
Postgres. The QA environment (its own Cloud Run service + Supabase project + deployed-QA
Playwright jobs) was dropped for cost, mirroring organize-me's 2026-09-08 change; the Playwright
`e2e/` suite is now local-only. `JWT_SECRET`, `ENCRYPTION_KEY`, and the two OAuth client *secrets*
(Slice R7) are wired into the prod deploy via `--set-secrets` from GCP Secret Manager —
`jwt-secret-prod` and `encryption-key-prod` are the SAME secrets the Host reads (no network call
between services, just the same signing/encryption key); `google-oauth-client-secret-prod` and
`dropbox-oauth-client-secret-prod` are specific to this service. The OAuth client *ids* (not
confidential) go through the plaintext env-vars-file like `DATABASE_URL`.

Required GitHub secrets on this repo: `GCP_SA_KEY`, `SUPABASE_PROD_URL`, `JWT_SECRET_PROD` (used
by both `test` jobs, not the deploy itself), `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`DROPBOX_OAUTH_CLIENT_ID`, `DROPBOX_OAUTH_CLIENT_SECRET`, `ENCRYPTION_KEY` (this last one only used
by the `test` job — tests inject their own throwaway Fernet keys regardless of its value).

**Human setup required before `deploy-prod` will succeed** (Slice R7): create
`google-oauth-client-secret-prod` and `dropbox-oauth-client-secret-prod` in GCP Secret Manager
(seeded with the same values as the `GOOGLE_OAUTH_CLIENT_SECRET`/`DROPBOX_OAUTH_CLIENT_SECRET`
GitHub secrets above), grant the Cloud Run runtime service account
`roles/secretmanager.secretAccessor` on each, and add this service's OAuth callback URLs
(`https://event-creator-prod-*.run.app/api/v1/storage-config/google-drive/callback` and the
`/dropbox/callback` equivalent, plus the LB custom-domain callback) as additional authorized
redirect URIs on the existing Google/Dropbox OAuth app consoles — the client id/secret are shared
with the Host's own registered app, just with a second redirect URI added.
`encryption-key-prod` already exists in Secret Manager (created in Slice R6) and needs no new
setup.

After the first deploy, re-run `organize-me`'s `infra/gcp_lb/provision.sh` (or `.ps1`) to attach
this service's NEG/backend to the shared Load Balancer's URL map.

## Deployed services

- Prod: `event-creator-prod` (Cloud Run, `northamerica-northeast1`) — first deployed 2026-07-12,
  deploys on every push to `main` via `deploy.yml`.
