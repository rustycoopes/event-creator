"""Unit tests for S3StorageProvider (#94), driven via a hand-rolled fake boto3 S3 client.

These exercise the provider's key-prefix and copy+delete move logic without touching AWS - no live
credentials are used in CI, per the issue's acceptance criteria.
"""

import io
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.services.storage.base import FileDestination, RemoteFile
from app.services.storage.s3 import S3Error, S3StorageProvider, _normalize_prefix


class FakeS3Client:
    """Records calls and serves objects from an in-memory dict, standing in for boto3's S3 client.

    Only implements the subset of the boto3 client surface S3StorageProvider actually calls, with
    just enough of each response shape (e.g. ``IsTruncated``/``NextContinuationToken``) to exercise
    pagination.
    """

    def __init__(self, *, page_size: int | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._page_size = page_size

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", kwargs))
        prefix = kwargs.get("Prefix", "")
        delimiter = kwargs.get("Delimiter")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        if delimiter:
            # Mimic S3: anything with another delimiter past the prefix is grouped into
            # CommonPrefixes instead of Contents (the fake doesn't bother returning
            # CommonPrefixes itself - the provider never reads it).
            keys = [k for k in keys if delimiter not in k[len(prefix) :]]
        token = kwargs.get("ContinuationToken")
        start = int(token) if token else 0
        page_size = self._page_size or len(keys)
        page = keys[start : start + page_size]
        is_truncated = start + page_size < len(keys)
        response: dict[str, Any] = {"Contents": [{"Key": k} for k in page]}
        if is_truncated:
            response["IsTruncated"] = True
            response["NextContinuationToken"] = str(start + page_size)
        return response

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("copy_object", kwargs))
        source_key = kwargs["CopySource"]["Key"]
        self.objects[kwargs["Key"]] = self.objects[source_key]
        return {}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs))
        del self.objects[kwargs["Key"]]
        return {}

    def close(self) -> None:
        self.closed = True


def _provider(client: FakeS3Client, folder_path: str = "OrganizeMe") -> S3StorageProvider:
    return S3StorageProvider(client=client, bucket_name="my-bucket", folder_path=folder_path)


def test_normalize_prefix_strips_slashes_and_adds_trailing_slash() -> None:
    assert _normalize_prefix("OrganizeMe") == "OrganizeMe/"
    assert _normalize_prefix("/OrganizeMe") == "OrganizeMe/"
    assert _normalize_prefix("/OrganizeMe/") == "OrganizeMe/"
    assert _normalize_prefix("/") == ""
    assert _normalize_prefix("") == ""


async def test_upload_file_puts_object_under_the_watched_prefix() -> None:
    client = FakeS3Client()
    provider = _provider(client)

    result = await provider.upload_file("chat.txt", b"hello")

    assert result == RemoteFile(id="OrganizeMe/chat.txt", name="chat.txt")
    assert client.objects["OrganizeMe/chat.txt"] == b"hello"
    assert client.calls[0] == (
        "put_object",
        {"Bucket": "my-bucket", "Key": "OrganizeMe/chat.txt", "Body": b"hello"},
    )


async def test_list_new_files_excludes_processed_and_failed_and_the_prefix_itself() -> None:
    client = FakeS3Client()
    client.objects = {
        "OrganizeMe/": b"",
        "OrganizeMe/chat1.txt": b"a",
        "OrganizeMe/chat2.txt": b"b",
        "OrganizeMe/processed/old.txt": b"c",
        "OrganizeMe/failed/bad.txt": b"d",
    }
    provider = _provider(client)

    files = await provider.list_new_files()

    assert sorted(files, key=lambda f: f.name) == [
        RemoteFile(id="OrganizeMe/chat1.txt", name="chat1.txt"),
        RemoteFile(id="OrganizeMe/chat2.txt", name="chat2.txt"),
    ]


async def test_list_new_files_excludes_nested_subfolder_keys() -> None:
    """Regression test: list_objects_v2's Prefix alone is recursive, unlike Dropbox's
    list_folder(recursive=False) or Google Drive's folder listing - a key nested another level
    below the watched prefix must not be treated as a direct child."""
    client = FakeS3Client()
    client.objects = {
        "OrganizeMe/chat1.txt": b"a",
        "OrganizeMe/nested/chat2.txt": b"b",
    }
    provider = _provider(client)

    files = await provider.list_new_files()

    assert files == [RemoteFile(id="OrganizeMe/chat1.txt", name="chat1.txt")]


async def test_list_new_files_follows_pagination() -> None:
    client = FakeS3Client(page_size=1)
    client.objects = {"OrganizeMe/chat1.txt": b"a", "OrganizeMe/chat2.txt": b"b"}
    provider = _provider(client)

    files = await provider.list_new_files()

    assert sorted(f.name for f in files) == ["chat1.txt", "chat2.txt"]
    list_calls = [c for c in client.calls if c[0] == "list_objects_v2"]
    assert len(list_calls) == 2
    assert "ContinuationToken" not in list_calls[0][1]
    assert list_calls[1][1]["ContinuationToken"] == "1"


async def test_download_file_returns_bytes() -> None:
    client = FakeS3Client()
    client.objects["OrganizeMe/chat.txt"] = b"file-contents"
    provider = _provider(client)

    content = await provider.download_file(RemoteFile(id="OrganizeMe/chat.txt", name="chat.txt"))

    assert content == b"file-contents"


async def test_move_file_copies_then_deletes_the_original() -> None:
    client = FakeS3Client()
    client.objects["OrganizeMe/chat.txt"] = b"data"
    provider = _provider(client)

    await provider.move_file(RemoteFile(id="OrganizeMe/chat.txt", name="chat.txt"), FileDestination.PROCESSED)

    assert "OrganizeMe/chat.txt" not in client.objects
    assert client.objects["OrganizeMe/processed/chat.txt"] == b"data"
    copy_call = next(c for c in client.calls if c[0] == "copy_object")
    assert copy_call[1] == {
        "Bucket": "my-bucket",
        "CopySource": {"Bucket": "my-bucket", "Key": "OrganizeMe/chat.txt"},
        "Key": "OrganizeMe/processed/chat.txt",
    }
    delete_call = next(c for c in client.calls if c[0] == "delete_object")
    assert delete_call[1] == {"Bucket": "my-bucket", "Key": "OrganizeMe/chat.txt"}


async def test_move_file_to_failed_destination() -> None:
    client = FakeS3Client()
    client.objects["OrganizeMe/chat.txt"] = b"data"
    provider = _provider(client)

    await provider.move_file(RemoteFile(id="OrganizeMe/chat.txt", name="chat.txt"), FileDestination.FAILED)

    assert client.objects["OrganizeMe/failed/chat.txt"] == b"data"


async def test_api_error_is_wrapped_in_s3_error() -> None:
    class FailingClient(FakeS3Client):
        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2")

    provider = _provider(FailingClient())

    with pytest.raises(S3Error):
        await provider.list_new_files()


async def test_aclose_closes_the_underlying_client() -> None:
    client = FakeS3Client()
    provider = _provider(client)

    await provider.aclose()

    assert client.closed is True
