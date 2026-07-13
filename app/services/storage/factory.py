"""Build the ``StorageProvider`` for a user's upload/pipeline run (Slice 4.1, #52).

Central place that turns a user's saved ``storage_configs`` row into a concrete provider:

- Under ``E2E_TEST_MODE`` (QA only) it returns the in-memory ``FakeStorageProvider`` so the
  Playwright suite (#53) can drive upload -> pipeline -> events without a real Drive connection or
  live OAuth (per #52's resolved testability decision).
- Otherwise it decrypts the stored Google Drive OAuth tokens (via ``CredentialCipher``, #47) and
  builds a ``GoogleDriveStorageProvider`` pointed at the user's watched folder.
- If storage config is unavailable or decryption fails (issue #79), falls back to
  ``EphemeralStorageProvider`` (in-memory, non-persistent) so uploads can still proceed.

The same provider instance is used for the upload write and then handed to the background pipeline
task, so its underlying HTTP client lives for the whole run.
"""

import logging

import boto3
import httpx

from app.core.config import Settings
from app.core.security import CredentialCipher
from app.models.storage_config import StorageConfig, StorageProviderType
from app.services.storage.base import StorageProvider
from app.services.storage.dropbox import DropboxStorageProvider
from app.services.storage.ephemeral import EphemeralStorageProvider
from app.services.storage.fake import FakeStorageProvider
from app.services.storage.google_drive import GoogleDriveStorageProvider
from app.services.storage.s3 import S3StorageProvider

logger = logging.getLogger(__name__)

# Generous per-call timeout: Drive/Dropbox uploads/downloads of a chat export are small, but the
# refresh + API round-trips shouldn't hang a background task forever.
_DRIVE_HTTP_TIMEOUT = httpx.Timeout(30.0)


def build_google_drive_provider(
    config: StorageConfig, settings: Settings, cipher: CredentialCipher
) -> GoogleDriveStorageProvider:
    """Construct a live Google Drive provider from a connected storage config."""
    access_token = (
        cipher.decrypt(config.oauth_access_token) if config.oauth_access_token else None
    )
    refresh_token = (
        cipher.decrypt(config.oauth_refresh_token) if config.oauth_refresh_token else None
    )
    return GoogleDriveStorageProvider(
        client=httpx.AsyncClient(timeout=_DRIVE_HTTP_TIMEOUT),
        folder_path=config.folder_path,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=config.oauth_token_expires_at,
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
    )


def build_dropbox_provider(
    config: StorageConfig, settings: Settings, cipher: CredentialCipher
) -> DropboxStorageProvider:
    """Construct a live Dropbox provider from a connected storage config."""
    access_token = (
        cipher.decrypt(config.oauth_access_token) if config.oauth_access_token else None
    )
    refresh_token = (
        cipher.decrypt(config.oauth_refresh_token) if config.oauth_refresh_token else None
    )
    return DropboxStorageProvider(
        client=httpx.AsyncClient(timeout=_DRIVE_HTTP_TIMEOUT),
        folder_path=config.folder_path,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=config.oauth_token_expires_at,
        client_id=settings.dropbox_oauth_client_id,
        client_secret=settings.dropbox_oauth_client_secret,
    )


def build_s3_provider(config: StorageConfig, cipher: CredentialCipher) -> S3StorageProvider:
    """Construct a live S3 provider from a connected storage config.

    All four fields are decrypted (per the Slice 8 spec, every credential column on
    ``storage_configs`` - including bucket name and region, not just the access key/secret - is
    stored encrypted at rest) and required: a config with ``provider = s3`` is only ever created
    once all four are populated (#95's write path).
    """
    access_key = cipher.decrypt(config.s3_access_key) if config.s3_access_key else None
    secret_key = cipher.decrypt(config.s3_secret_key) if config.s3_secret_key else None
    bucket_name = cipher.decrypt(config.s3_bucket_name) if config.s3_bucket_name else None
    region = cipher.decrypt(config.s3_region) if config.s3_region else None
    if not (access_key and secret_key and bucket_name and region):
        raise ValueError("S3 storage config is missing required credentials")
    client = boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    return S3StorageProvider(client=client, bucket_name=bucket_name, folder_path=config.folder_path)


def build_storage_provider(
    *,
    config: StorageConfig | None,
    settings: Settings,
    cipher: CredentialCipher | None,
    fallback_to_ephemeral: bool = False,
) -> StorageProvider:
    """Resolve the provider for a run: the E2E fake under test mode, else a real provider chosen
    by the config's ``provider`` column.

    ``config`` and ``cipher`` are required for the real path; callers gate on a connected config
    before reaching it (see app.api.v1.upload).

    If ``fallback_to_ephemeral`` is True and no config/cipher is available, returns an
    EphemeralStorageProvider instead of raising. Used for issue #79 graceful degradation.
    """
    if settings.e2e_test_mode:
        return FakeStorageProvider()
    if config is None or cipher is None:
        if fallback_to_ephemeral:
            logger.warning(
                "storage config unavailable, falling back to ephemeral (in-memory) storage"
            )
            return EphemeralStorageProvider()
        # pragma: no cover - guarded by the caller
        raise ValueError("a storage config and cipher are required outside E2E_TEST_MODE")
    if config.provider == StorageProviderType.DROPBOX:
        return build_dropbox_provider(config, settings, cipher)
    if config.provider == StorageProviderType.GOOGLE_DRIVE:
        return build_google_drive_provider(config, settings, cipher)
    if config.provider == StorageProviderType.S3:
        return build_s3_provider(config, cipher)
    raise ValueError(f"unsupported storage provider: {config.provider}")
