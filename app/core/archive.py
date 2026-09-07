"""Content-based archive handling for WhatsApp exports (whatsapp-archive-import Slice 1, #47).

A WhatsApp export is a ZIP archive that routinely arrives *without* a ``.zip`` extension - or as a
single-file gzip stream, or renamed to anything at all. This module answers "is this an archive,
and get the chat text out of it" purely from the bytes: magic-byte sniffing, member selection,
bounded decompression, BOM-aware decoding. No I/O, no DB, plain exceptions - the same idiom as
``message_filter`` / ``date_parser``.

The pipeline runner's Extract step is the sole authoritative caller (it classifies every path to
Gemini); the Upload endpoint calls ``sniff_archive`` only as an advisory fast-fail. See
``docs/adr/whatsapp-archive-import-detection-and-module.md`` and
``docs/adr/whatsapp-archive-import-decompression-cap.md``.
"""

import gzip
import io
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

# Sized for the pipeline's 512 MiB Cloud Run instance - a decompressed member plus its decoded
# ``str`` (2-4x for CJK/Arabic/Hebrew) plus the filtered text plus the Gemini payload must all fit.
# Enforced on the *actual read*, never on ``ZipInfo.file_size`` / the gzip ISIZE trailer (both are
# attacker-controlled metadata). See the decompression-cap ADR.
MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024

_NO_CHAT = "The archive doesn't contain a readable chat text file."
_TOO_LARGE = "The archive's contents are too large to process."
_UNREADABLE = "The archive could not be read - it may be corrupt or password-protected."


class ArchiveError(Exception):
    """No readable chat text could be produced from the given bytes."""


@dataclass(frozen=True)
class ChatExtraction:
    text: str  # decoded chat text, ready for the date filter
    member_name: str  # for the Extract step log
    reason: str  # e.g. "extracted _chat.txt", "decompressed gzip"


def sniff_archive(content: bytes) -> Literal["zip", "gzip"] | None:
    """Classify ``content`` by its magic bytes only - never by any filename."""
    if zipfile.is_zipfile(io.BytesIO(content)):
        return "zip"
    if content[:2] == b"\x1f\x8b":
        return "gzip"
    return None


def decode_text(content: bytes) -> str:
    """Decode chat-export bytes to ``str``, BOM-aware.

    A UTF-16 BOM (WhatsApp exports on some platforms) decodes as ``utf-16``; everything else as
    ``utf-8-sig`` (which strips a UTF-8 BOM) with ``errors="replace"``. A leading BOM char that
    survives is stripped so ``message_filter``'s ``^``-anchored patterns match line 1. Applied at
    both the archive path and the runner's non-archive path so a UTF-16 export can't be mangled by
    one and not the other.
    """
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = content.decode("utf-16", errors="replace")
    else:
        text = content.decode("utf-8-sig", errors="replace")
    return text.lstrip("﻿")


def _within_cap(data: bytes) -> bytes:
    if len(data) > MAX_DECOMPRESSED_BYTES:
        raise ArchiveError(_TOO_LARGE)
    return data


def extract_chat_text(content: bytes, *, kind: str, source_name: str) -> ChatExtraction:
    """Unpack an archive and return its chat text. ``kind`` is ``sniff_archive``'s result;
    ``source_name`` names the gzip member only. Raises ``ArchiveError`` when no readable chat text
    can be produced (corrupt/encrypted archive, media-only archive, an Office document that happens
    to be a valid ZIP, an over-cap member)."""
    if kind == "gzip":
        return _extract_gzip(content, source_name)
    if kind == "zip":
        return _extract_zip(content)
    raise ArchiveError(_UNREADABLE)  # pragma: no cover - kind always comes from sniff_archive


def _extract_gzip(content: bytes, source_name: str) -> ChatExtraction:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
            # ponytail: whole member into memory, bounded at 20 MB (see the zip read site).
            raw = _within_cap(stream.read(MAX_DECOMPRESSED_BYTES + 1))
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveError(_UNREADABLE) from exc
    text = decode_text(raw)
    if "\x00" in text:
        raise ArchiveError(_NO_CHAT)
    if source_name.lower().endswith(".gz"):
        name = source_name[: -len(".gz")]
    else:
        name = f"{source_name}.txt"
    return ChatExtraction(text=text, member_name=name, reason="decompressed gzip")


def _extract_zip(content: bytes) -> ChatExtraction:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(_UNREADABLE) from exc

    with archive:
        # Regular-file entries only: no directory entries, no macOS resource-fork cruft
        # (``__MACOSX/`` paths, ``._``-prefixed basenames). PurePosixPath, never os.path - zip
        # member names are always forward-slash.
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and "__MACOSX/" not in info.filename
            and not PurePosixPath(info.filename).name.startswith("._")
        ]
        if not members:
            raise ArchiveError(_NO_CHAT)

        def read(info: zipfile.ZipInfo) -> bytes:
            try:
                with archive.open(info) as stream:
                    # ponytail: the whole member is pulled into memory (bounded at 20 MB by the
                    # read + _within_cap). Streaming its lines straight into message_filter is
                    # the real fix - see docs/adr/whatsapp-archive-import-decompression-cap.md.
                    return _within_cap(stream.read(MAX_DECOMPRESSED_BYTES + 1))
            except ArchiveError:
                raise
            except (
                RuntimeError,
                NotImplementedError,
                zipfile.BadZipFile,
                zlib.error,
                OSError,
                EOFError,
            ) as exc:
                # RuntimeError: encrypted entry, no password. NotImplementedError: unsupported
                # compression method (deflate64, imploded, ...). The rest: truncated/corrupt.
                raise ArchiveError(_UNREADABLE) from exc

        # 1. Exact _chat.txt (case-insensitive basename) - the iOS export.
        for info in members:
            if PurePosixPath(info.filename).name.lower() == "_chat.txt":
                return ChatExtraction(decode_text(read(info)), info.filename, "extracted _chat.txt")

        # 2. Largest *.txt, ties broken by name - the Android "WhatsApp Chat with X.txt" export.
        txts = sorted(
            (i for i in members if PurePosixPath(i.filename).name.lower().endswith(".txt")),
            key=lambda i: (-i.file_size, i.filename),
        )
        if txts:
            return ChatExtraction(
                decode_text(read(txts[0])), txts[0].filename, "used largest .txt file"
            )

        # 3. A member with a plain, dot-free basename that decodes to NUL-free text (a chat file
        #    that lost its extension). Anything with a dot in its basename - Office XML / `.rels`
        #    in a .docx-shaped ZIP, media files, dotfiles - is excluded so its contents never
        #    reach Gemini (PRD story 5); those fall through to the ArchiveError below.
        for info in members:
            if "." in PurePosixPath(info.filename).name:
                continue
            text = decode_text(read(info))
            if "\x00" not in text:
                return ChatExtraction(text, info.filename, "used the only readable text file")

        # 4. Nothing readable - media-only archive, or a valid ZIP that isn't a chat export.
        raise ArchiveError(_NO_CHAT)
