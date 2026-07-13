"""Concrete S3 storage backend (Slice 8.2, #94).

Implements the ``StorageProvider`` contract (app.services.storage.base, #45) against a user's
manually-entered AWS credentials (access key, secret key, bucket, region) rather than OAuth. Uses
the synchronous ``boto3`` SDK wrapped in ``asyncio.to_thread`` for every blocking call - mirrors the
pattern in app.services.notifications.email.ResendEmailSender, which runs the blocking Resend SDK
call the same way - rather than adding ``aioboto3`` as a dependency.

Objects are addressed by their S3 key (used directly as ``RemoteFile.id``). ``folder_path`` (the
same watch-folder column shared with the other providers) is treated as a key prefix within the
bucket. S3 has no folders or a native move operation, so ``move_file`` is implemented as
copy-then-delete into ``processed/``/``failed/`` sub-prefixes under that watched prefix.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.services.storage.base import FileDestination, RemoteFile, StorageProvider

logger = logging.getLogger(__name__)


class S3Error(RuntimeError):
    """An S3 API call failed (network, auth, or unexpected response) - wraps the underlying
    botocore exception so callers depend on this module's error type rather than botocore's,
    same as DropboxError does for the Dropbox provider."""


def _normalize_prefix(path: str) -> str:
    """S3 keys never start with '/', and a non-empty prefix must end with '/' so a listing under
    it doesn't also match sibling keys sharing the same string prefix (e.g. "Chats" would otherwise
    also match "ChatsArchive/foo.txt")."""
    stripped = path.strip("/")
    return f"{stripped}/" if stripped else ""


class S3StorageProvider(StorageProvider):
    def __init__(
        self,
        *,
        client: Any,
        bucket_name: str,
        folder_path: str,
    ) -> None:
        self._client = client
        self._bucket = bucket_name
        self._prefix = _normalize_prefix(folder_path)

    def _destination_prefix(self, destination: FileDestination) -> str:
        return f"{self._prefix}{destination.value}/"

    async def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """Run a blocking boto3 call off the event loop, wrapping any failure in S3Error."""
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise S3Error(f"S3 call {fn.__name__} failed: {exc}") from exc

    # -- StorageProvider contract -----------------------------------------------------------

    async def upload_file(self, name: str, content: bytes) -> RemoteFile:
        key = f"{self._prefix}{name}"
        await self._call(self._client.put_object, Bucket=self._bucket, Key=key, Body=content)
        return RemoteFile(id=key, name=name)

    async def list_new_files(self) -> list[RemoteFile]:
        # Delimiter="/" makes S3 group anything past the next "/" into CommonPrefixes instead of
        # Contents, so processed/failed sub-prefixes (and any other nested "subfolder") are
        # excluded by construction - the same non-recursive, direct-children-only semantics as
        # Dropbox's list_folder(recursive=False) and the Google Drive provider.
        files: list[RemoteFile] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": self._prefix,
                "Delimiter": "/",
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = await self._call(self._client.list_objects_v2, **kwargs)
            for obj in response.get("Contents", []):
                key = obj["Key"]
                if key == self._prefix:
                    continue
                files.append(RemoteFile(id=key, name=key[len(self._prefix) :]))
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
        return files

    async def download_file(self, file: RemoteFile) -> bytes:
        response = await self._call(self._client.get_object, Bucket=self._bucket, Key=file.id)
        body = response["Body"]
        return await asyncio.to_thread(body.read)

    async def move_file(self, file: RemoteFile, destination: FileDestination) -> None:
        dest_key = f"{self._destination_prefix(destination)}{file.name}"
        await self._call(
            self._client.copy_object,
            Bucket=self._bucket,
            CopySource={"Bucket": self._bucket, "Key": file.id},
            Key=dest_key,
        )
        await self._call(self._client.delete_object, Bucket=self._bucket, Key=file.id)

    async def aclose(self) -> None:
        """Close the underlying boto3 client so its connection pool is released after a run."""
        await asyncio.to_thread(self._client.close)
