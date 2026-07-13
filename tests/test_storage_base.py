"""Tests for the StorageProvider contract and its in-memory fake (issue #45)."""

import pytest

from app.services.storage.base import FileDestination, RemoteFile, StorageProvider
from app.services.storage.fake import FakeStorageProvider


def test_storage_provider_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        StorageProvider()  # type: ignore[abstract]


def test_storage_provider_declares_the_expected_abstract_methods() -> None:
    assert StorageProvider.__abstractmethods__ == {
        "upload_file",
        "list_new_files",
        "download_file",
        "move_file",
    }


async def test_fake_provider_satisfies_the_contract() -> None:
    report = RemoteFile(id="1", name="chat-export.txt")
    archive = RemoteFile(id="2", name="backup.zip")
    provider = FakeStorageProvider({report: b"hello world", archive: b"PK\x03\x04"})

    assert isinstance(provider, StorageProvider)
    assert set(await provider.list_new_files()) == {report, archive}
    assert await provider.download_file(report) == b"hello world"


async def test_fake_provider_move_removes_file_from_new_and_records_destination() -> None:
    report = RemoteFile(id="1", name="chat-export.txt")
    archive = RemoteFile(id="2", name="backup.zip")
    provider = FakeStorageProvider({report: b"a", archive: b"b"})

    await provider.move_file(report, FileDestination.PROCESSED)

    assert await provider.list_new_files() == [archive]
    assert provider.moved == {"1": FileDestination.PROCESSED}
