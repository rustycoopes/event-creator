"""Concrete Dropbox storage backend (Slice 8.1, #93).

Implements the ``StorageProvider`` contract (app.services.storage.base, #45) against the Dropbox
API v2, using the OAuth tokens a user connected via app.api.v1.storage_dropbox (stored encrypted on
their ``storage_configs`` row; decrypted by the caller before this is constructed). Calls go through
an injected ``httpx.AsyncClient`` - same pattern as google_drive.py - rather than the official
(synchronous) ``dropbox`` SDK, so testing mocks the httpx transport and the dependency footprint
stays async-first.

Files are addressed by Dropbox's own stable ``id:...`` identifier (not by path), since paths change
when a file is renamed or moved - Dropbox's API accepts an id anywhere a path is expected.

The short-lived access token is refreshed from the refresh token on demand (when missing, expired,
or a call returns 401), same as the Google Drive provider - see that module's docstring for why
this is in-memory-only per provider instance.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.services.storage.base import FileDestination, RemoteFile, StorageProvider

logger = logging.getLogger(__name__)

DROPBOX_API = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT_API = "https://content.dropboxapi.com/2"
DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"  # noqa: S105 - public OAuth endpoint, not a secret
# Dropbox rejects a "path/conflict/folder/..." error when the destination already exists; that's
# the expected steady-state once processed/failed have been created once, not a real failure.
_FOLDER_CONFLICT_ERROR_TAG = "path/conflict/folder/"
# Refresh a little before the token actually expires so an in-flight call doesn't race the expiry.
_EXPIRY_SKEW = timedelta(seconds=60)


class DropboxError(RuntimeError):
    """A Dropbox API call failed (network, auth, or unexpected response)."""


def _normalize_path(path: str) -> str:
    """Dropbox's root folder is addressed as ``""``, never ``"/"``, and every non-root path must
    start with ``"/"`` - unlike Google Drive, which resolves ``folder_path`` by splitting on ``/``
    regardless of a leading slash (the same `storage_configs.folder_path` column, and its write-path
    validator, is shared across all providers and doesn't guarantee Dropbox's stricter shape)."""
    stripped = path.rstrip("/")
    if not stripped:
        return ""
    return stripped if stripped.startswith("/") else f"/{stripped}"


class DropboxStorageProvider(StorageProvider):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        folder_path: str,
        access_token: str | None,
        refresh_token: str | None,
        token_expires_at: datetime | None,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._client = client
        self._folder_path = _normalize_path(folder_path)
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at
        self._client_id = client_id
        self._client_secret = client_secret
        # Destination subfolders (processed/failed) are created on first use and cached for the
        # provider's lifetime, mirroring the Drive provider's folder-id cache.
        self._ensured_destinations: set[FileDestination] = set()

    # -- auth -------------------------------------------------------------------------------

    def _token_is_fresh(self) -> bool:
        if not self._access_token:
            return False
        if self._token_expires_at is None:
            return True  # no expiry known - assume usable, a 401 will force a refresh
        return datetime.now(timezone.utc) + _EXPIRY_SKEW < self._token_expires_at

    async def _refresh_access_token(self) -> None:
        if not self._refresh_token:
            raise DropboxError("no refresh token available to obtain Dropbox access")
        response = await self._client.post(
            DROPBOX_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code != httpx.codes.OK:
            raise DropboxError(f"token refresh failed ({response.status_code})")
        payload = response.json()
        self._access_token = payload["access_token"]
        expires_in = payload.get("expires_in")
        self._token_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in
            else None
        )

    async def _auth_headers(self) -> dict[str, str]:
        if not self._token_is_fresh():
            await self._refresh_access_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _raw_request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Issue an authenticated Dropbox request, refreshing once and retrying on a 401. Returns
        the response as-is (even on a non-401 error) so a caller that expects a specific error
        (e.g. "folder already exists") can inspect it before deciding whether to raise."""
        headers = {**(await self._auth_headers()), **(kwargs.pop("headers", {}) or {})}  # type: ignore[dict-item]
        response = await self._client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._refresh_access_token()
            # The fresh Authorization must win over the stale one still in `headers` from the
            # first attempt - spreading it last here (not first) is what makes the retry actually
            # use the just-refreshed token instead of repeating the same 401.
            headers = {**headers, **(await self._auth_headers())}
            response = await self._client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        return response

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Like ``_raw_request``, but raises on any remaining error status."""
        response = await self._raw_request(method, url, **kwargs)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise DropboxError(f"Dropbox API {method} {url} failed ({response.status_code})")
        return response

    # -- folder resolution ------------------------------------------------------------------

    def _destination_path(self, destination: FileDestination) -> str:
        return f"{self._folder_path}/{destination.value}"

    async def _ensure_destination_folder(self, destination: FileDestination) -> str:
        """Create the ``processed/``/``failed/`` subfolder if it doesn't exist yet, and return its
        path. Cached for the provider's lifetime once confirmed to exist."""
        path = self._destination_path(destination)
        if destination in self._ensured_destinations:
            return path
        response = await self._raw_request(
            "POST",
            f"{DROPBOX_API}/files/create_folder_v2",
            json={"path": path, "autorename": False},
        )
        if response.status_code == httpx.codes.OK or (
            response.status_code == httpx.codes.CONFLICT
            and _FOLDER_CONFLICT_ERROR_TAG in response.text
        ):
            self._ensured_destinations.add(destination)
            return path
        raise DropboxError(f"failed to create Dropbox folder '{path}' ({response.status_code})")

    # -- StorageProvider contract -----------------------------------------------------------

    async def upload_file(self, name: str, content: bytes) -> RemoteFile:
        response = await self._request(
            "POST",
            f"{DROPBOX_CONTENT_API}/files/upload",
            headers={
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": json.dumps(
                    {"path": f"{self._folder_path}/{name}", "mode": "add", "autorename": True}
                ),
            },
            content=content,
        )
        body = response.json()
        return RemoteFile(id=str(body["id"]), name=str(body["name"]))

    async def list_new_files(self) -> list[RemoteFile]:
        files: list[RemoteFile] = []
        response = await self._request(
            "POST",
            f"{DROPBOX_API}/files/list_folder",
            json={"path": self._folder_path, "recursive": False},
        )
        while True:
            body = response.json()
            files.extend(
                RemoteFile(id=str(entry["id"]), name=str(entry["name"]))
                for entry in body.get("entries", [])
                if entry.get(".tag") == "file"
            )
            if not body.get("has_more"):
                break
            response = await self._request(
                "POST",
                f"{DROPBOX_API}/files/list_folder/continue",
                json={"cursor": body["cursor"]},
            )
        return files

    async def download_file(self, file: RemoteFile) -> bytes:
        response = await self._request(
            "POST",
            f"{DROPBOX_CONTENT_API}/files/download",
            headers={"Dropbox-API-Arg": json.dumps({"path": file.id})},
        )
        return response.content

    async def move_file(self, file: RemoteFile, destination: FileDestination) -> None:
        dest_folder = await self._ensure_destination_folder(destination)
        await self._request(
            "POST",
            f"{DROPBOX_API}/files/move_v2",
            json={
                "from_path": file.id,
                "to_path": f"{dest_folder}/{file.name}",
                "autorename": True,
            },
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client so its connection pool is released after a run."""
        await self._client.aclose()
