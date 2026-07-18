# ---- Build stage: compiles the Tailwind CSS build. The ~35-40MB Tailwind CLI binary and the
# build-only Python packages that fetch it never reach the runtime image below - see
# docs/adr/design-refresh-per-service-tailwind-build.md (organize-me). The compile step (below) is
# a separate RUN layer from `COPY app`, but Docker's cache invalidation still cascades correctly:
# any change under app/ invalidates `COPY app` and therefore forces the compile layer to rerun too,
# so cached CSS can never silently drift out of sync with the templates it was built from.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --group build --no-install-project

COPY app ./app
COPY scripts ./scripts
RUN uv run python scripts/build_css.py

# ---- Runtime stage: no Tailwind CLI, no build-only Python packages - only the compiled
# stylesheet and fonts are copied in from the builder stage.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# git is required at build time because organizeme-chrome is a git dependency - uv sync needs
# git on PATH to resolve/clone it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY --from=builder /app/app/static/css/app.css ./app/static/css/app.css
COPY --from=builder /app/app/static/fonts ./app/static/fonts

RUN uv sync --frozen --no-dev

EXPOSE 8080

# Cloud Tasks (Slice R11 redesign) replaced the Celery worker that used to run alongside this
# process via supervisord - pipeline execution now happens inside a normal request to
# POST /internal/pipeline/run, handled by this same uvicorn process. No second program to
# supervise, so supervisord itself is no longer needed here.
#
# Listens on Cloud Run's injected $PORT (defaults to 8080 for a fresh service). Wrapped in
# /bin/sh -c because CMD's exec form does not perform shell/env-var expansion on its own.
#
# --forwarded-allow-ips='*' trusts the X-Forwarded-Proto header from whatever peer connects to
# the container. Cloud Run terminates TLS at its own front end and always proxies to the
# container over a private, single-hop connection - the container is never reachable except
# through that proxy - so this is safe here.
CMD ["/bin/sh", "-c", "/app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
