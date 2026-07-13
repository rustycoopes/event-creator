"""The storage-provider contract every backend (Google Drive now; Dropbox/S3 later) implements.

Built as part of Slice 2.0 (issue #45) so the settings UI (#46), Google Drive OAuth (#47), and
the processing pipeline (Slice 4) can all depend on one interface rather than provider-specific
code. The concrete Google Drive implementation and the real pipeline wiring land in later issues;
this module defines only the abstract contract plus the value types it trades in.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class RemoteFile:
    """A file discovered in the user's watched storage folder.

    `id` is the provider's own stable identifier (a Drive file id, an S3 key, ...); `name` is
    the human-facing filename used for logging and the processed/failed move.
    """

    id: str
    name: str


class FileDestination(str, Enum):
    """Where a file is moved after processing. Providers map these onto their own
    `processed/` and `failed/` subfolders."""

    PROCESSED = "processed"
    FAILED = "failed"


class StorageProvider(ABC):
    """Abstract interface for a cloud storage backend OrganizeMe watches for new export files.

    All methods are async: every concrete provider performs network I/O, and the app is
    async-first. Implementations must not log or expose raw credentials.
    """

    @abstractmethod
    async def upload_file(self, name: str, content: bytes) -> RemoteFile:
        """Write ``content`` into the watched folder as ``name`` and return the created file.

        Used by the manual-upload path (Slice 4.1): the uploaded bytes land in the user's watch
        folder so the pipeline processes and then moves them exactly like a file the user dropped
        there themselves."""
        ...

    @abstractmethod
    async def list_new_files(self) -> list[RemoteFile]:
        """Return the files currently in the watched folder that still need processing
        (i.e. not already under a processed/ or failed/ subfolder)."""
        ...

    @abstractmethod
    async def download_file(self, file: RemoteFile) -> bytes:
        """Return the raw bytes of `file` for the processing pipeline to parse."""
        ...

    @abstractmethod
    async def move_file(self, file: RemoteFile, destination: FileDestination) -> None:
        """Move `file` into the provider's processed/ or failed/ subfolder once handled."""
        ...

    async def aclose(self) -> None:
        """Release any resources the provider holds (e.g. an HTTP client).

        Concrete default is a no-op so in-memory providers (the fake) need not implement it; the
        pipeline calls it once a run finishes so a network-backed provider can close its client
        instead of leaking a connection pool per upload."""
        return None
