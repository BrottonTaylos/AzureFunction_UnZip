"""Read a zip archive straight out of Blob Storage, without downloading it.

`BlobRangeReader` exposes a blob as a seekable, read-only file object backed by
HTTP range requests, so `zipfile` can read the central directory from the end
of the blob and then stream individual entries. Nothing is written to disk.

`plan_entries` validates every entry before anything is uploaded: unsafe paths,
unsupported compression, encryption and zip-bomb style ratios are rejected up
front so a one-off run fails fast instead of half-way through.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass

from azure.core import MatchConditions

# Compression methods the Python zipfile module can decompress.
SUPPORTED_COMPRESSION = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
    zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA,
}
COMPRESSION_NAMES = {9: "Deflate64", 99: "AES-encrypted", 93: "Zstandard (unsupported here)", 98: "PPMd"}

# Characters SharePoint Online does not allow in file or folder names.
_SHAREPOINT_INVALID = re.compile(r'["*:<>?\\|\x00-\x1f]')
_SHAREPOINT_RESERVED = {
    ".lock", "con", "prn", "aux", "nul", "desktop.ini",
    *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10)),
}
# SharePoint rejects decoded paths longer than this (site-relative, incl. library/folder).
MAX_SHAREPOINT_PATH = 400


class ZipValidationError(ValueError):
    """The archive contains entries that cannot be transferred safely."""


@dataclass(frozen=True)
class Limits:
    max_entries: int = 200_000
    max_total_uncompressed: int = 2 * 1024**4  # 2 TiB
    max_ratio: int = 1_000  # uncompressed / compressed, checked for entries over 1 MiB


class BlobRangeReader(io.RawIOBase):
    """Seekable read-only view of a blob, pinned to the ETag seen at open time.

    Pinning the ETag means the run fails rather than mixing two versions of the
    archive if the blob is overwritten mid-transfer. Wrap in `io.BufferedReader`
    (see `open_blob_zip`) so zipfile's many small reads become few large requests.
    """

    def __init__(self, blob_client):
        super().__init__()
        self._blob = blob_client
        props = blob_client.get_blob_properties()
        self._size = props.size
        self._etag = props.etag
        self._pos = 0

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if pos < 0:
            raise ValueError("negative seek position")
        self._pos = pos
        return pos

    def readinto(self, buffer) -> int:
        length = min(len(buffer), self._size - self._pos)
        if length <= 0:
            return 0
        data = self._blob.download_blob(
            offset=self._pos,
            length=length,
            etag=self._etag,
            match_condition=MatchConditions.IfNotModified,
            max_concurrency=4,
        ).readall()
        n = len(data)
        buffer[:n] = data
        self._pos += n
        return n


def open_blob_zip(blob_client, buffer_size: int = 8 * 1024 * 1024) -> zipfile.ZipFile:
    reader = io.BufferedReader(BlobRangeReader(blob_client), buffer_size=buffer_size)
    return zipfile.ZipFile(reader)


def sharepoint_relative_path(entry_name: str) -> str:
    """Map a zip entry name to a safe, SharePoint-valid relative path.

    Raises ZipValidationError for absolute paths or `..` traversal; replaces
    characters SharePoint does not allow with `_`.
    """
    name = entry_name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ZipValidationError("absolute path")
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ZipValidationError("path traversal ('..')")
        part = _SHAREPOINT_INVALID.sub("_", part).strip().rstrip(".") or "_"
        part = re.sub("_vti_", "_vti-", part, flags=re.IGNORECASE)
        if part.lower() in _SHAREPOINT_RESERVED or posixpath.splitext(part)[0].lower() in _SHAREPOINT_RESERVED:
            part = f"_{part}"
        if part.startswith("~$"):
            part = "_" + part[1:]
        parts.append(part)
    if not parts:
        raise ZipValidationError("empty name")
    return "/".join(parts)


def plan_entries(
    zf: zipfile.ZipFile,
    limits: Limits,
    has_password: bool = False,
    target_folder: str = "",
) -> list[dict]:
    """Validate every entry and return the list of files to transfer.

    Each item is {"index", "target", "size"}; `index` is the position in
    `zf.infolist()` so an activity can re-open exactly that entry, and `target`
    is the path inside `target_folder`. All problems are collected and raised
    together as one ZipValidationError.
    """
    infos = zf.infolist()
    if len(infos) > limits.max_entries:
        raise ZipValidationError(f"{len(infos)} entries exceeds limit of {limits.max_entries}")

    problems: list[str] = []
    planned: list[dict] = []
    seen: dict[str, int] = {}
    total = 0

    for index, info in enumerate(infos):
        if info.is_dir():
            continue  # folders are created implicitly by the uploads
        label = f"entry #{index}"
        try:
            target = sharepoint_relative_path(info.filename)
        except ZipValidationError as exc:
            problems.append(f"{label}: {exc}")
            continue
        if info.compress_type not in SUPPORTED_COMPRESSION:
            method = COMPRESSION_NAMES.get(info.compress_type, f"method {info.compress_type}")
            problems.append(f"{label}: unsupported compression {method}")
        if info.flag_bits & 0x1 and not has_password:
            problems.append(f"{label}: encrypted but no ZIP_PASSWORD configured")
        if (
            info.file_size > 1024 * 1024
            and info.file_size > limits.max_ratio * max(info.compress_size, 1)
        ):
            problems.append(f"{label}: compression ratio above {limits.max_ratio}:1")
        if len(posixpath.join(target_folder, target)) > MAX_SHAREPOINT_PATH:
            problems.append(f"{label}: path longer than {MAX_SHAREPOINT_PATH} characters")
        key = target.lower()  # SharePoint names are case-insensitive
        if key in seen:
            problems.append(f"{label}: target path collides with entry #{seen[key]}")
        seen[key] = index
        total += info.file_size
        planned.append({"index": index, "target": target, "size": info.file_size})

    if total > limits.max_total_uncompressed:
        problems.append(
            f"total uncompressed size {total} exceeds limit of {limits.max_total_uncompressed}"
        )
    if problems:
        shown = "; ".join(problems[:50])
        more = f" (and {len(problems) - 50} more)" if len(problems) > 50 else ""
        raise ZipValidationError(f"{len(problems)} problem(s): {shown}{more}")
    return planned
