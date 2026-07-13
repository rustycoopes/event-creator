"""Ephemeral in-memory storage provider - fallback when primary storage is unavailable.

Used as a graceful fallback when Google Drive / Dropbox / S3 is not configured or
unavailable, allowing uploads to proceed without losing user data. Files are stored
in memory for the duration of the processing run, then discarded (never persisted).
Successfully processed files are marked as such but don't actually move anywhere.

This is a temporary solution for the current single-user architecture (issue #79);
a production fallback would use Cloud Storage or a managed queue instead.
"""

import uuid

from app.services.storage.base import FileDestination, RemoteFile, StorageProvider


class EphemeralStorageProvider(StorageProvider):
    """In-memory file storage with no persistence.

    Intended as a graceful fallback when a user's primary storage (Google Drive, Dropbox, S3)
    is not configured or unavailable. Files uploaded are stored in memory and returned during
    list_new_files() until explicitly moved, but no actual file storage persists beyond the
    processing run's lifetime.
    """

    def __init__(self) -> None:
        # file id -> content bytes
        self._files: dict[RemoteFile, bytes] = {}
        # file id -> destination it was moved to; also removes it from list_new_files
        self.moved: dict[str, FileDestination] = {}
        # Names uploaded, in order, for diagnostics
        self.uploaded: list[str] = []

    async def upload_file(self, name: str, content: bytes) -> RemoteFile:
        """Store a file in memory."""
        file = RemoteFile(id=str(uuid.uuid4()), name=name)
        self._files[file] = content
        self.uploaded.append(name)
        return file

    async def list_new_files(self) -> list[RemoteFile]:
        """Return files that haven't been moved yet (not in processed/ or failed/)."""
        return [f for f in self._files if f.id not in self.moved]

    async def download_file(self, file: RemoteFile) -> bytes:
        """Retrieve a file's content from memory."""
        return self._files[file]

    async def move_file(self, file: RemoteFile, destination: FileDestination) -> None:
        """Record that a file has been processed or failed (no actual move)."""
        self.moved[file.id] = destination
