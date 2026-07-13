"""Tests for the ephemeral (in-memory) storage provider fallback (issue #79)."""

from app.services.storage.base import FileDestination
from app.services.storage.ephemeral import EphemeralStorageProvider


async def test_ephemeral_provider_stores_and_retrieves_files() -> None:
    """Files uploaded to ephemeral storage can be downloaded immediately."""
    provider = EphemeralStorageProvider()
    content = b"test conversation data"

    remote_file = await provider.upload_file("chat.txt", content)

    assert remote_file.name == "chat.txt"
    downloaded = await provider.download_file(remote_file)
    assert downloaded == content


async def test_ephemeral_provider_lists_new_files() -> None:
    """list_new_files() returns only files that haven't been moved yet."""
    provider = EphemeralStorageProvider()
    file1 = await provider.upload_file("chat1.txt", b"data1")
    file2 = await provider.upload_file("chat2.txt", b"data2")

    # Both should be listed as new.
    new_files = await provider.list_new_files()
    assert len(new_files) == 2
    assert file1 in new_files
    assert file2 in new_files


async def test_ephemeral_provider_removes_moved_files_from_list() -> None:
    """Files moved to processed/failed are excluded from list_new_files()."""
    provider = EphemeralStorageProvider()
    file1 = await provider.upload_file("chat1.txt", b"data1")
    file2 = await provider.upload_file("chat2.txt", b"data2")

    # Move file1 to processed.
    await provider.move_file(file1, FileDestination.PROCESSED)

    # Only file2 should be listed now.
    new_files = await provider.list_new_files()
    assert len(new_files) == 1
    assert file2 in new_files
    assert file1 not in new_files


async def test_ephemeral_provider_tracks_uploaded_names() -> None:
    """Provider tracks uploaded filenames for diagnostics."""
    provider = EphemeralStorageProvider()

    await provider.upload_file("chat1.txt", b"data1")
    await provider.upload_file("chat2.zip", b"data2")

    assert provider.uploaded == ["chat1.txt", "chat2.zip"]


async def test_ephemeral_provider_with_multiple_moves() -> None:
    """Provider correctly handles multiple files moved to different destinations."""
    provider = EphemeralStorageProvider()
    file1 = await provider.upload_file("chat1.txt", b"data1")
    file2 = await provider.upload_file("chat2.txt", b"data2")
    file3 = await provider.upload_file("chat3.txt", b"data3")

    await provider.move_file(file1, FileDestination.PROCESSED)
    await provider.move_file(file2, FileDestination.FAILED)

    # Only file3 should be listed.
    new_files = await provider.list_new_files()
    assert len(new_files) == 1
    assert file3 in new_files

    # Verify the move destinations are tracked.
    assert provider.moved[file1.id] == FileDestination.PROCESSED
    assert provider.moved[file2.id] == FileDestination.FAILED
