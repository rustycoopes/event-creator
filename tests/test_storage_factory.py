"""Unit tests for app.services.storage.factory's provider-resolution branching (ported, #93).

Confirms build_storage_provider picks the concrete provider class matching a config's
``provider`` column, rather than always resolving to Google Drive.
"""

import uuid

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.security import CredentialCipher
from app.models.storage_config import StorageConfig, StorageProviderType
from app.services.storage.dropbox import DropboxStorageProvider
from app.services.storage.factory import build_storage_provider
from app.services.storage.google_drive import GoogleDriveStorageProvider
from app.services.storage.s3 import S3StorageProvider

_CIPHER = CredentialCipher(Fernet.generate_key())


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        jwt_secret="secret",
        google_oauth_client_id="g-id",
        google_oauth_client_secret="g-secret",
        dropbox_oauth_client_id="d-id",
        dropbox_oauth_client_secret="d-secret",
        e2e_test_mode=False,
    )


def _config(provider: StorageProviderType) -> StorageConfig:
    return StorageConfig(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        provider=provider,
        folder_path="/OrganizeMe",
    )


def _s3_config() -> StorageConfig:
    config = _config(StorageProviderType.S3)
    config.s3_access_key = _CIPHER.encrypt("access-key")
    config.s3_secret_key = _CIPHER.encrypt("secret-key")
    config.s3_bucket_name = _CIPHER.encrypt("my-bucket")
    config.s3_region = _CIPHER.encrypt("us-east-1")
    return config


def test_dropbox_config_resolves_to_dropbox_provider() -> None:
    provider = build_storage_provider(
        config=_config(StorageProviderType.DROPBOX),
        settings=_settings(),
        cipher=_CIPHER,
    )
    assert isinstance(provider, DropboxStorageProvider)


def test_google_drive_config_resolves_to_google_drive_provider() -> None:
    provider = build_storage_provider(
        config=_config(StorageProviderType.GOOGLE_DRIVE),
        settings=_settings(),
        cipher=_CIPHER,
    )
    assert isinstance(provider, GoogleDriveStorageProvider)


def test_s3_config_resolves_to_s3_provider() -> None:
    provider = build_storage_provider(
        config=_s3_config(),
        settings=_settings(),
        cipher=_CIPHER,
    )
    assert isinstance(provider, S3StorageProvider)


def test_s3_config_decrypts_credentials_into_the_boto3_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: build_s3_provider must pass the *decrypted* values to boto3.client, not
    the still-encrypted column values - a mismatch here would only surface as an opaque AWS auth
    failure at pipeline-run time rather than a test failure."""
    captured: dict[str, object] = {}

    def fake_client(service_name: str, **kwargs: object) -> object:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.services.storage.factory.boto3.client", fake_client)

    build_storage_provider(config=_s3_config(), settings=_settings(), cipher=_CIPHER)

    assert captured == {
        "service_name": "s3",
        "aws_access_key_id": "access-key",
        "aws_secret_access_key": "secret-key",
        "region_name": "us-east-1",
    }


def test_s3_config_missing_credentials_raises() -> None:
    with pytest.raises(ValueError, match="missing required credentials"):
        build_storage_provider(
            config=_config(StorageProviderType.S3),
            settings=_settings(),
            cipher=_CIPHER,
        )
