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

RUN uv sync --frozen --no-dev

EXPOSE 8080

# Listens on Cloud Run's injected $PORT (defaults to 8080 for a fresh service, unlike the Host's
# organizeme-qa/prod, which had containerPort=8000 set on an earlier deploy and kept it since -
# event-creator has no such inherited config, so it must follow Cloud Run's actual default rather
# than assume a hardcoded port). --forwarded-allow-ips='*' trusts X-Forwarded-Proto from Cloud
# Run's edge proxy (TLS-terminating, single-hop, never directly reachable).
CMD ["/bin/sh", "-c", "/app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
