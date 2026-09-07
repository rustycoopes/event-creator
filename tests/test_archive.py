"""Unit tests for app.core.archive (whatsapp-archive-import Slice 1, #47).

Pure functions, no DB, no I/O: every archive is synthesised in-test with ``zipfile`` / ``gzip``.
The over-cap case ``monkeypatch``es ``MAX_DECOMPRESSED_BYTES`` down to ~1 KB rather than allocating
20 MB.
"""

import base64
import gzip
import io
import zipfile

import pytest

from app.core import archive
from app.core.archive import (
    ArchiveError,
    ChatExtraction,
    decode_text,
    extract_chat_text,
    sniff_archive,
)

_CHAT = "5/30/26, 10:00 - Russ: hi\n5/31/26, 09:00 - Sam: yo\n"

# A traditional-PKWARE-encrypted ZIP holding one stored `_chat.txt` (Python's zipfile cannot
# *write* encrypted archives; this was baked by scratchpad/mkzip.py). Reading its member without a
# password raises RuntimeError inside zipfile.
_ENCRYPTED_ZIP = base64.b64decode(
    "UEsDBBQAAQAAAAAAAAALjJjZJQAAABkAAAAJAAAAX2NoYXQudHh0va8dDUUGwFTq4mu9G0wFIvuUgtfv"
    "qgIVoog47ySvsJfUcFOuXFBLAQIUABQAAQAAAAAAAAALjJjZJQAAABkAAAAJAAAAAAAAAAAAAAAAAAAA"
    "AABfY2hhdC50eHRQSwUGAAAAAAEAAQA3AAAATAAAAAAA"
)


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buffer.getvalue()


# --- sniff_archive -------------------------------------------------------------------------

def test_sniff_detects_zip_by_content_not_name() -> None:
    assert sniff_archive(_zip([("whatever", b"x")])) == "zip"


def test_sniff_detects_gzip_by_magic_bytes() -> None:
    assert sniff_archive(gzip.compress(b"hello")) == "gzip"


def test_sniff_returns_none_for_plain_text() -> None:
    assert sniff_archive(_CHAT.encode()) is None
    assert sniff_archive(b"") is None


# --- decode_text --------------------------------------------------------------------------

def test_decode_plain_utf8() -> None:
    assert decode_text("héllo".encode("utf-8")) == "héllo"


def test_decode_strips_utf8_bom() -> None:
    assert decode_text("﻿héllo".encode("utf-8")) == "héllo"


def test_decode_utf16_bom_without_mojibake() -> None:
    assert decode_text("héllo — 😀".encode("utf-16")) == "héllo — 😀"


def test_decode_replaces_undecodable_bytes_rather_than_raising() -> None:
    assert "�" in decode_text(b"ok \xff\xfe\x00 tail" + b"\x81\x82")


# --- ZIP member selection ---------------------------------------------------------------

def test_exact_chat_txt_wins_over_media_regardless_of_order() -> None:
    members = [
        ("IMG-0001.jpg", b"\xff\xd8\xff\xe0 jpeg"),
        ("_chat.txt", _CHAT.encode()),
        ("PTT-0002.opus", b"OggS\x00 audio"),
    ]
    for ordering in (members, list(reversed(members))):
        result = extract_chat_text(_zip(ordering), kind="zip", source_name="x")
        assert result.text == _CHAT
        assert result.member_name == "_chat.txt"
        assert "_chat.txt" in result.reason


def test_chat_txt_matched_case_insensitively() -> None:
    result = extract_chat_text(_zip([("_CHAT.TXT", _CHAT.encode())]), kind="zip", source_name="x")
    assert result.text == _CHAT


def test_largest_txt_used_when_no_chat_txt() -> None:
    result = extract_chat_text(
        _zip(
            [
                ("readme.txt", b"tiny"),
                ("WhatsApp Chat with Alice.txt", _CHAT.encode()),
            ]
        ),
        kind="zip",
        source_name="x",
    )
    assert result.member_name == "WhatsApp Chat with Alice.txt"
    assert result.text == _CHAT


def test_txt_tie_broken_by_name_deterministically() -> None:
    same = b"x" * 10
    result = extract_chat_text(
        _zip([("b.txt", same), ("a.txt", same)]), kind="zip", source_name="x"
    )
    assert result.member_name == "a.txt"


def test_extensionless_text_member_is_the_fallback() -> None:
    result = extract_chat_text(_zip([("chatlog", _CHAT.encode())]), kind="zip", source_name="x")
    assert result.text == _CHAT
    assert result.member_name == "chatlog"


def test_macosx_and_dotunderscore_entries_are_ignored() -> None:
    result = extract_chat_text(
        _zip(
            [
                ("__MACOSX/._chat.txt", b"\x00\x01 resource fork"),
                ("._chat.txt", b"\x00\x01 resource fork"),
                ("_chat.txt", _CHAT.encode()),
            ]
        ),
        kind="zip",
        source_name="x",
    )
    assert result.text == _CHAT


def test_media_only_archive_raises_archive_error() -> None:
    with pytest.raises(ArchiveError, match="readable chat text"):
        extract_chat_text(
            _zip([("IMG-1.jpg", b"\xff\xd8\xff data"), ("v.mp4", b"\x00\x00\x00 ftyp")]),
            kind="zip",
            source_name="x",
        )


def test_docx_shaped_zip_raises_rather_than_feeding_xml_downstream() -> None:
    docx = _zip(
        [
            ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
            ("_rels/.rels", b"<?xml version='1.0'?><Relationships/>"),
            ("word/document.xml", b"<?xml version='1.0'?><document>hi</document>"),
        ]
    )
    assert sniff_archive(docx) == "zip"
    with pytest.raises(ArchiveError, match="readable chat text"):
        extract_chat_text(docx, kind="zip", source_name="report.docx")


def test_encrypted_zip_raises_understandable_error() -> None:
    assert sniff_archive(_ENCRYPTED_ZIP) == "zip"
    with pytest.raises(ArchiveError, match="corrupt or password-protected"):
        extract_chat_text(_ENCRYPTED_ZIP, kind="zip", source_name="x")


def test_truncated_zip_raises_understandable_error() -> None:
    good = _zip([("_chat.txt", _CHAT.encode() * 50)])
    with pytest.raises(ArchiveError):
        extract_chat_text(good[: len(good) // 2], kind="zip", source_name="x")


def test_unsupported_compression_method_raises_not_escapes() -> None:
    """A renamed ZIP using a method zipfile can't inflate (deflate64, imploded, ...) makes
    ``ZipFile.open`` raise ``NotImplementedError``; it must come back as ``ArchiveError``, not
    escape and leave the pipeline run stuck non-terminal."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_chat.txt", "hi there")
    raw = bytearray(buffer.getvalue())
    for marker, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        i = raw.find(marker)
        raw[i + offset : i + offset + 2] = (9).to_bytes(2, "little")  # method 9 = deflate64
    with pytest.raises(ArchiveError):
        extract_chat_text(bytes(raw), kind="zip", source_name="x")


def test_empty_zip_raises_archive_error() -> None:
    with pytest.raises(ArchiveError, match="readable chat text"):
        extract_chat_text(_zip([]), kind="zip", source_name="x")


# --- gzip -------------------------------------------------------------------------------

def test_gzip_with_gz_extension_names_member_without_it() -> None:
    result = extract_chat_text(
        gzip.compress(_CHAT.encode()), kind="gzip", source_name="_chat.txt.gz"
    )
    assert result.text == _CHAT
    assert result.member_name == "_chat.txt"
    assert result.reason == "decompressed gzip"


def test_gzip_without_gz_extension_appends_txt() -> None:
    result = extract_chat_text(gzip.compress(_CHAT.encode()), kind="gzip", source_name="export")
    assert result.member_name == "export.txt"
    assert result.text == _CHAT


def test_gzip_of_binary_content_raises() -> None:
    with pytest.raises(ArchiveError):
        extract_chat_text(gzip.compress(b"\x00\x01\x02\x00 not text"), kind="gzip", source_name="x")


def test_corrupt_gzip_raises_understandable_error() -> None:
    with pytest.raises(ArchiveError, match="corrupt"):
        extract_chat_text(b"\x1f\x8b\x08\x00 garbage that is not deflate", kind="gzip", source_name="x")


# --- decompression cap ----------------------------------------------------------------

def test_over_cap_zip_member_raises_size_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_DECOMPRESSED_BYTES", 1024)
    big = _zip([("_chat.txt", b"a" * 4096)])  # compresses tiny, expands past the 1 KB cap
    with pytest.raises(ArchiveError, match="too large"):
        extract_chat_text(big, kind="zip", source_name="x")


def test_over_cap_gzip_stream_raises_size_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_DECOMPRESSED_BYTES", 1024)
    with pytest.raises(ArchiveError, match="too large"):
        extract_chat_text(gzip.compress(b"a" * 4096), kind="gzip", source_name="x")


def test_shrunk_size_metadata_does_not_bypass_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decompression-bomb archive that lies in its size metadata is still rejected - the guard
    is on the actual read, never on ``ZipInfo.file_size`` (decompression-cap ADR)."""
    monkeypatch.setattr(archive, "MAX_DECOMPRESSED_BYTES", 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_chat.txt", b"a" * 8192)
    raw = bytearray(buffer.getvalue())
    for marker, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        i = raw.find(marker)
        raw[i + offset : i + offset + 4] = (10).to_bytes(4, "little")  # claim 10 bytes
    with pytest.raises(ArchiveError):
        extract_chat_text(bytes(raw), kind="zip", source_name="x")


def test_chat_extraction_is_immutable() -> None:
    result = ChatExtraction(text="a", member_name="b", reason="c")
    with pytest.raises(Exception):
        result.text = "z"  # type: ignore[misc]
