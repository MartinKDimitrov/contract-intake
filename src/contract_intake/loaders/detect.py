"""File type detection from content, not from what the sender claimed.

``Content-Type`` in a mail part is whatever the sending client felt like
writing. It is routinely wrong (``application/octet-stream`` for everything is
common) and occasionally a lie. Since the declared type decides whether we spend
a vision-priced model call, it is sniffed from the first bytes instead.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class FileKind(StrEnum):
    PDF = "pdf"
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    ZIP = "zip"
    OFFICE = "office"
    UNKNOWN = "unknown"


IMAGE_KINDS = frozenset({FileKind.PNG, FileKind.JPEG, FileKind.TIFF})

MIME_BY_KIND: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.PNG: "image/png",
    FileKind.JPEG: "image/jpeg",
    FileKind.TIFF: "image/tiff",
    FileKind.ZIP: "application/zip",
    FileKind.OFFICE: "application/vnd.openxmlformats-officedocument",
    FileKind.UNKNOWN: "application/octet-stream",
}

_SIGNATURES: tuple[tuple[bytes, FileKind], ...] = (
    (b"%PDF-", FileKind.PDF),
    (b"\x89PNG\r\n\x1a\n", FileKind.PNG),
    (b"\xff\xd8\xff", FileKind.JPEG),
    (b"II*\x00", FileKind.TIFF),
    (b"MM\x00*", FileKind.TIFF),
)


def sniff(head: bytes) -> FileKind:
    """Identify a file from its leading bytes."""
    for signature, kind in _SIGNATURES:
        if head.startswith(signature):
            return kind

    # Office formats are ZIP containers; the member names give them away.
    if head.startswith(b"PK\x03\x04"):
        window = head[:2048]
        if b"word/" in window or b"xl/" in window or b"ppt/" in window:
            return FileKind.OFFICE
        return FileKind.ZIP

    return FileKind.UNKNOWN


def sniff_path(path: Path, *, read_bytes: int = 2048) -> FileKind:
    try:
        with path.open("rb") as handle:
            return sniff(handle.read(read_bytes))
    except OSError:
        return FileKind.UNKNOWN
