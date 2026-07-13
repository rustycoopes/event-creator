from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.storage_config import StorageProviderType


class StorageConfigRead(BaseModel):
    """The current user's storage config as returned by ``GET /storage-config``.

    Deliberately exposes only ``provider``, ``folder_path`` and the derived ``is_connected`` flag:
    every credential column on the model (OAuth tokens, S3 keys) is a write-only secret encrypted
    at rest and must never be echoed back over the API. ``provider``/``folder_path`` are ``None``
    when the user has no config yet - the "unset" state the settings page renders as empty.
    """

    model_config = ConfigDict(from_attributes=True)

    provider: StorageProviderType | None = None
    folder_path: str | None = None
    # Whether the provider is actually authenticated (an OAuth token is stored). Always false in
    # this slice - the connect flow that sets it lands in issue #47 - but surfaced now so the
    # Storage tab can show connection state without a later schema change.
    is_connected: bool = False


class StorageConfigWrite(BaseModel):
    """Payload for ``PUT /storage-config``: the user's chosen provider + watch-folder path.

    Credentials are never set here - the OAuth connect flow (issue #47) populates the encrypted
    token columns separately. ``folder_path`` is required and non-empty because the underlying
    column is ``NOT NULL``.
    """

    provider: StorageProviderType
    folder_path: str = Field(min_length=1, max_length=1024)

    @field_validator("folder_path")
    @classmethod
    def _strip_and_require_non_blank(cls, value: str) -> str:
        # min_length=1 only rejects the empty string, so a whitespace-only path ("   ") would slip
        # through and be stored verbatim. Trim surrounding whitespace and reject a now-blank value.
        stripped = value.strip()
        if not stripped:
            raise ValueError("folder_path must not be blank")
        return stripped
