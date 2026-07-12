from sqlalchemy.engine import make_url


def to_asyncpg_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+asyncpg")
    # asyncpg.connect() has no `sslmode` kwarg (only `ssl`); libpq-style connection strings
    # (the format Postgres tooling usually documents/copy-pastes) commonly include it.
    if "sslmode" in url.query:
        url = url.set(query={k: v for k, v in url.query.items() if k != "sslmode"})
    return url.render_as_string(hide_password=False)
