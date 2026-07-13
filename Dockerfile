FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# git is required at build time because organizeme-chrome is a git dependency - uv sync needs
# git on PATH to resolve/clone it. supervisor runs the web (uvicorn) and worker (celery)
# processes side by side in this one container, mirroring the monolith's supervisord.conf
# pattern (Slice R8) - unlike the monolith, both programs are autostart=true here since
# REDIS_URL is actually wired through to Cloud Run for this service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY supervisord.conf ./supervisord.conf

RUN uv sync --frozen --no-dev

EXPOSE 8080

# supervisord.conf's [program:web] preserves this same $PORT-listening / --forwarded-allow-ips
# behavior (wrapped in /bin/sh -c there too, since Cloud Run injects $PORT and defaults to 8080
# for a fresh service - event-creator has no inherited containerPort=8000 config like the Host's
# organizeme-qa/prod). [program:worker] runs `celery -A app.worker worker --loglevel=info`
# alongside it, both supervised so either crashing restarts it rather than killing the container.
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
