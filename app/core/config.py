from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    database_url: str
    # Same signing key as the Host (Secret Manager secret `jwt-secret-qa`/`jwt-secret-prod`, R4) —
    # Event Creator verifies the Host-issued JWT, it never issues one of its own.
    jwt_secret: str


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
