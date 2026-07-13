"""Unit tests for GoogleDriveStorageProvider (#52), driven via httpx.MockTransport.

These exercise the provider's request-building and token-refresh logic without touching Google.
The live end-to-end behaviour (real Drive folders + OAuth) is out of CI scope and verified by
manual QA against a connected account - see the PR note.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.storage.base import FileDestination, RemoteFile
from app.services.storage.google_drive import GoogleDriveStorageProvider

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _provider(handler: object, *, expired: bool = False) -> GoogleDriveStorageProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=-1 if expired else 1
    )
    return GoogleDriveStorageProvider(
        client=client,
        folder_path="/OrganizeMe",
        access_token="access-token",
        refresh_token="refresh-token",
        token_expires_at=expires_at,
        client_id="client-id",
        client_secret="client-secret",
    )


def _folder_lookup(request: httpx.Request) -> httpx.Response | None:
    """Answer a files.list folder-resolution query for the watch folder."""
    q = request.url.params.get("q", "")
    if "name = 'OrganizeMe'" in q:
        return httpx.Response(200, json={"files": [{"id": "watch123", "name": "OrganizeMe"}]})
    return None


async def test_upload_file_resolves_folder_then_uploads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        folder = _folder_lookup(request)
        if folder is not None:
            return folder
        if request.method == "POST" and request.url.path == "/drive/v3/files":
            body = json.loads(request.content)
            assert body == {"name": "chat.txt", "parents": ["watch123"]}
            return httpx.Response(200, json={"id": "file1", "name": "chat.txt"})
        if request.method == "PATCH" and request.url.path == "/upload/drive/v3/files/file1":
            assert request.url.params.get("uploadType") == "media"
            assert request.content == b"hello"
            return httpx.Response(200, json={"id": "file1", "name": "chat.txt"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    result = await provider.upload_file("chat.txt", b"hello")

    assert result == RemoteFile(id="file1", name="chat.txt")


async def test_download_file_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/file1" and request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"file-contents")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    content = await provider.download_file(RemoteFile(id="file1", name="chat.txt"))

    assert content == b"file-contents"


async def test_move_file_adds_and_removes_parents() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        folder = _folder_lookup(request)
        if folder is not None:
            return folder
        q = request.url.params.get("q", "")
        if "name = 'processed'" in q:  # destination subfolder doesn't exist yet
            return httpx.Response(200, json={"files": []})
        if request.method == "POST" and request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"id": "processed1"})  # created subfolder
        if request.method == "PATCH" and request.url.path == "/drive/v3/files/file1":
            seen["patch"] = request
            return httpx.Response(200, json={"id": "file1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    await provider.move_file(RemoteFile(id="file1", name="chat.txt"), FileDestination.PROCESSED)

    patch = seen["patch"]
    assert patch.url.params.get("addParents") == "processed1"
    assert patch.url.params.get("removeParents") == "watch123"


async def test_expired_token_is_refreshed_before_use() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            calls.append("refresh")
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        # Every Drive call must carry the refreshed bearer token.
        assert request.headers["Authorization"] == "Bearer fresh-token"
        folder = _folder_lookup(request)
        if folder is not None:
            return folder
        return httpx.Response(200, json={"files": []})

    provider = _provider(handler, expired=True)
    await provider.list_new_files()

    assert calls == ["refresh"]


async def test_api_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    provider = _provider(handler)
    with pytest.raises(Exception):
        await provider.list_new_files()


async def test_aclose_closes_the_http_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    provider = _provider(handler)
    await provider.aclose()

    assert provider._client.is_closed
