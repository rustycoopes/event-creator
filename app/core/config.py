from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    database_url: str
    # Same signing key as the Host (Secret Manager secret `jwt-secret-qa`/`jwt-secret-prod`, R4) —
    # Event Creator verifies the Host-issued JWT, it never issues one of its own.
    jwt_secret: str
    # Google Drive OAuth app credentials (Slice R7, ported from organize-me's storage-connect
    # flow). Empty defaults so deploys/CI that don't set them yet don't fail Settings
    # construction - the connect flow only needs real values once a user actually clicks Connect.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Dropbox OAuth app credentials (Slice R7). Same empty-default reasoning.
    dropbox_oauth_client_id: str = ""
    dropbox_oauth_client_secret: str = ""
    # Fernet key used to encrypt stored storage-provider credentials at rest (see
    # app.core.security). Empty default (mirrors organize-me's ENCRYPTION_KEY) - the storage
    # connect flows raise a clear, actionable RuntimeError if it's actually used while unset.
    # Must be a urlsafe-base64 32-byte key (cryptography.fernet.Fernet.generate_key()).
    encryption_key: str = ""
    # Enables the E2E fake storage provider in app.services.storage.factory.build_storage_provider
    # (mirrors organize-me's flag). Defaults false; no route in this slice reads it yet, but the
    # ported factory function's signature depends on it existing on Settings.
    e2e_test_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
