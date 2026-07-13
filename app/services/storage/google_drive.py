"""Concrete Google Drive storage backend (Slice 4.1, #52).

Implements the ``StorageProvider`` contract (app.services.storage.base, #45) against the Google
Drive REST API v3, using the OAuth tokens a user connected in #47 (stored encrypted on their
``storage_configs`` row; decrypted by the caller before this is constructed). It uploads the
manually-uploaded file into the user's watched folder, lists/downloads new files there, and moves a
handled file into a ``processed/`` or ``failed/`` subfolder.

Testability: every call goes through an injected ``httpx.AsyncClient``, so unit tests drive the
provider with an ``httpx.MockTransport`` and never touch Google. The live behaviour (real Drive
folders, real OAuth) is out of CI scope — see the manual-QA note in the PR; it can only be
exercised against a genuinely connected account.

The short-lived access token is refreshed from the refresh token on demand (when missing, expired,
or a call returns 401). Refreshes are in-memory for the provider's lifetime — a run creates a fresh
provider, so it refreshes at most once per run; persisting the rotated access token back to the DB
is a possible later optimisation, not required for correctness (the refresh token is long-lived).
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.services.storage.base import FileDestination, RemoteFile, StorageProvider

logger = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public OAuth endpoint, not a secret
_FOLDER_MIME = "application/vnd.google-apps.folder"
# Refresh a little before the token actually expires so an in-flight call doesn't race the expiry.
_EXPIRY_SKEW = timedelta(seconds=60)


class GoogleDriveError(RuntimeError):
    """A Google Drive API call failed (network, auth, or unexpected response)."""


def _escape_query_value(value: str) -> str:
    """Escape a value for a Drive ``q`` string literal (single-quote delimited)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveStorageProvider(StorageProvider):
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
        self._folder_path = folder_path
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at
        self._client_id = client_id
        self._client_secret = client_secret
        # Resolved lazily and cached for the provider's lifetime.
        self._watch_folder_id: str | None = None
        self._destination_folder_ids: dict[FileDestination, str] = {}

    # -- auth -------------------------------------------------------------------------------

    def _token_is_fresh(self) -> bool:
        if not self._access_token:
            return False
        if self._token_expires_at is None:
            return True  # no expiry known - assume usable, a 401 will force a refresh
        return datetime.now(timezone.utc) + _EXPIRY_SKEW < self._token_expires_at

    async def _refresh_access_token(self) -> None:
        if not self._refresh_token:
            raise GoogleDriveError("no refresh token available to obtain Drive access")
        response = await self._client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code != httpx.codes.OK:
            raise GoogleDriveError(f"token refresh failed ({response.status_code})")
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

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Issue an authenticated Drive request, refreshing once and retrying on a 401."""
        headers = {**(await self._auth_headers()), **(kwargs.pop("headers", {}) or {})}  # type: ignore[dict-item]
        response = await self._client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._refresh_access_token()
            headers = {**(await self._auth_headers()), **headers}
            response = await self._client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise GoogleDriveError(f"Drive API {method} {url} failed ({response.status_code})")
        return response

    # -- folder resolution ------------------------------------------------------------------

    async def _find_child_folder(self, parent_id: str, name: str) -> str | None:
        query = (
            f"'{_escape_query_value(parent_id)}' in parents and "
            f"name = '{_escape_query_value(name)}' and "
            f"mimeType = '{_FOLDER_MIME}' and trashed = false"
        )
        response = await self._request(
            "GET", f"{DRIVE_API}/files", params={"q": query, "fields": "files(id,name)"}
        )
        files = response.json().get("files", [])
        return str(files[0]["id"]) if files else None

    async def _create_child_folder(self, parent_id: str, name: str) -> str:
        response = await self._request(
            "POST",
            f"{DRIVE_API}/files",
            json={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
        )
        return str(response.json()["id"])

    async def _watch_folder(self) -> str:
        """Resolve the configured folder path to a Drive folder id, traversing from My Drive."""
        if self._watch_folder_id is not None:
            return self._watch_folder_id
        parent = "root"
        for segment in [s for s in self._folder_path.split("/") if s]:
            found = await self._find_child_folder(parent, segment)
            if found is None:
                raise GoogleDriveError(f"watch folder segment '{segment}' not found in Drive")
            parent = found
        self._watch_folder_id = parent
        return parent

    async def _destination_folder(self, destination: FileDestination) -> str:
        """The id of the ``processed/`` or ``failed/`` subfolder of the watch folder, created on
        first use if it doesn't exist yet."""
        if destination in self._destination_folder_ids:
            return self._destination_folder_ids[destination]
        watch_id = await self._watch_folder()
        folder_id = await self._find_child_folder(
            watch_id, destination.value
        ) or await self._create_child_folder(watch_id, destination.value)
        self._destination_folder_ids[destination] = folder_id
        return folder_id

    # -- StorageProvider contract -----------------------------------------------------------

    async def upload_file(self, name: str, content: bytes) -> RemoteFile:
        """Create the file then upload its bytes as two requests.

        Deliberately not a single ``uploadType=multipart`` request: httpx's ``files=`` produces a
        ``multipart/form-data`` body, but Drive's multipart upload expects ``multipart/related``
        (RFC 2387) - sending the former is silently misinterpreted by Drive. The two-request
        approach (metadata-only create, then a plain ``uploadType=media`` body) sidesteps that
        encoding mismatch entirely and needs no hand-rolled multipart body.
        """
        watch_id = await self._watch_folder()
        metadata = {"name": name, "parents": [watch_id]}
        create_response = await self._request("POST", f"{DRIVE_API}/files", json=metadata)
        file_id = str(create_response.json()["id"])
        upload_response = await self._request(
            "PATCH",
            f"{DRIVE_UPLOAD_URL}/{file_id}",
            params={"uploadType": "media"},
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        body = upload_response.json()
        return RemoteFile(id=str(body["id"]), name=str(body.get("name", name)))

    async def list_new_files(self) -> list[RemoteFile]:
        watch_id = await self._watch_folder()
        query = (
            f"'{_escape_query_value(watch_id)}' in parents and trashed = false and "
            f"mimeType != '{_FOLDER_MIME}'"
        )
        response = await self._request(
            "GET", f"{DRIVE_API}/files", params={"q": query, "fields": "files(id,name)"}
        )
        return [RemoteFile(id=str(f["id"]), name=str(f["name"])) for f in response.json().get("files", [])]

    async def download_file(self, file: RemoteFile) -> bytes:
        response = await self._request(
            "GET", f"{DRIVE_API}/files/{file.id}", params={"alt": "media"}
        )
        return response.content

    async def move_file(self, file: RemoteFile, destination: FileDestination) -> None:
        watch_id = await self._watch_folder()
        dest_id = await self._destination_folder(destination)
        await self._request(
            "PATCH",
            f"{DRIVE_API}/files/{file.id}",
            params={"addParents": dest_id, "removeParents": watch_id},
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client so its connection pool is released after a run."""
        await self._client.aclose()
