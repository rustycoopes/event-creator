"""Unit tests for DropboxStorageProvider (#93), driven via httpx.MockTransport.

These exercise the provider's request-building and token-refresh logic without touching Dropbox.
Live end-to-end behaviour (a real Dropbox app + OAuth) is out of CI scope - see the PR note.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.storage.base import FileDestination, RemoteFile
from app.services.storage.dropbox import DropboxStorageProvider, _normalize_path


def _provider(handler: object, *, expired: bool = False) -> DropboxStorageProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    expires_at = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)
    return DropboxStorageProvider(
        client=client,
        folder_path="/OrganizeMe",
        access_token="access-token",
        refresh_token="refresh-token",
        token_expires_at=expires_at,
        client_id="client-id",
        client_secret="client-secret",
    )


def test_normalize_path_adds_a_leading_slash_when_missing() -> None:
    """Regression test: folder_path is a column shared across all three providers (its write-path
    validator only trims whitespace), so a value saved without a leading slash - valid for Google
    Drive's split-and-traverse resolution - must still become a Dropbox-valid path here."""
    assert _normalize_path("OrganizeMe") == "/OrganizeMe"
    assert _normalize_path("/OrganizeMe") == "/OrganizeMe"
    assert _normalize_path("/OrganizeMe/") == "/OrganizeMe"
    assert _normalize_path("/") == ""
    assert _normalize_path("") == ""


async def test_upload_file_posts_metadata_and_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/files/upload":
            arg = json.loads(request.headers["Dropbox-API-Arg"])
            assert arg == {"path": "/OrganizeMe/chat.txt", "mode": "add", "autorename": True}
            assert request.content == b"hello"
            return httpx.Response(200, json={"id": "id:file1", "name": "chat.txt"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    result = await provider.upload_file("chat.txt", b"hello")

    assert result == RemoteFile(id="id:file1", name="chat.txt")


async def test_list_new_files_returns_only_files_and_follows_pagination() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/files/list_folder":
            calls.append("list")
            body = json.loads(request.content)
            assert body == {"path": "/OrganizeMe", "recursive": False}
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {".tag": "file", "id": "id:file1", "name": "chat.txt"},
                        {".tag": "folder", "id": "id:folder1", "name": "processed"},
                    ],
                    "cursor": "cursor1",
                    "has_more": True,
                },
            )
        if request.url.path == "/2/files/list_folder/continue":
            calls.append("continue")
            body = json.loads(request.content)
            assert body == {"cursor": "cursor1"}
            return httpx.Response(
                200,
                json={
                    "entries": [{".tag": "file", "id": "id:file2", "name": "chat2.txt"}],
                    "has_more": False,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    files = await provider.list_new_files()

    assert files == [
        RemoteFile(id="id:file1", name="chat.txt"),
        RemoteFile(id="id:file2", name="chat2.txt"),
    ]
    assert calls == ["list", "continue"]


async def test_download_file_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/files/download":
            arg = json.loads(request.headers["Dropbox-API-Arg"])
            assert arg == {"path": "id:file1"}
            return httpx.Response(200, content=b"file-contents")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    content = await provider.download_file(RemoteFile(id="id:file1", name="chat.txt"))

    assert content == b"file-contents"


async def test_move_file_creates_destination_folder_then_moves() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/files/create_folder_v2":
            body = json.loads(request.content)
            assert body == {"path": "/OrganizeMe/processed", "autorename": False}
            return httpx.Response(200, json={"metadata": {"path_lower": "/organizeme/processed"}})
        if request.url.path == "/2/files/move_v2":
            seen["move"] = request
            return httpx.Response(200, json={"metadata": {}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    await provider.move_file(RemoteFile(id="id:file1", name="chat.txt"), FileDestination.PROCESSED)

    body = json.loads(seen["move"].content)
    assert body == {
        "from_path": "id:file1",
        "to_path": "/OrganizeMe/processed/chat.txt",
        "autorename": True,
    }


async def test_move_file_tolerates_destination_folder_already_existing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/files/create_folder_v2":
            return httpx.Response(
                409,
                json={"error_summary": "path/conflict/folder/", "error": {".tag": "path"}},
            )
        if request.url.path == "/2/files/move_v2":
            return httpx.Response(200, json={"metadata": {}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = _provider(handler)
    await provider.move_file(RemoteFile(id="id:file1", name="chat.txt"), FileDestination.FAILED)


async def test_expired_token_is_refreshed_before_use() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.dropboxapi.com" and request.url.path == "/oauth2/token":
            calls.append("refresh")
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fresh-token"
        return httpx.Response(200, json={"entries": [], "has_more": False})

    provider = _provider(handler, expired=True)
    await provider.list_new_files()

    assert calls == ["refresh"]


async def test_live_401_is_retried_with_the_refreshed_token() -> None:
    """Regression test: a token that's still "fresh" by the proactive expiry check but has been
    revoked/invalidated server-side must actually succeed on retry after a refresh, not resend the
    same stale Authorization header it just got a 401 for."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.dropboxapi.com" and request.url.path == "/oauth2/token":
            calls.append("refresh")
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        if request.headers["Authorization"] == "Bearer access-token":
            return httpx.Response(401, json={"error_summary": "expired_access_token/"})
        assert request.headers["Authorization"] == "Bearer fresh-token"
        return httpx.Response(200, json={"entries": [], "has_more": False})

    provider = _provider(handler)  # not expired per the proactive check - only a live 401 forces it
    await provider.list_new_files()

    assert calls == ["refresh"]


async def test_api_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error_summary": "boom"})

    provider = _provider(handler)
    with pytest.raises(Exception):
        await provider.list_new_files()


async def test_aclose_closes_the_http_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    provider = _provider(handler)
    await provider.aclose()

    assert provider._client.is_closed
