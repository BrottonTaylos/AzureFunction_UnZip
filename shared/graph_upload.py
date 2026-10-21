"""Upload streams to a SharePoint document library through Microsoft Graph.

Files are sent with resumable upload sessions in fixed-size chunks read
directly from the source stream, so memory use is one chunk per transfer and
any file size up to SharePoint's 250 GB limit works.
"""

from __future__ import annotations

import hashlib
import logging
import time
from urllib.parse import quote, urlsplit

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# Upload session chunks must be a multiple of 320 KiB and at most 60 MiB.
CHUNK_UNIT = 320 * 1024
DEFAULT_CHUNK_SIZE = 32 * CHUNK_UNIT  # 10 MiB
RETRY_STATUS = {429, 500, 502, 503, 504}

log = logging.getLogger(__name__)


class TargetExistsError(RuntimeError):
    """A file already exists at the target path and differs from the source."""


class GraphClient:
    def __init__(self, credential, session: requests.Session | None = None,
                 max_attempts: int = 6, backoff_seconds: float = 2.0):
        self._credential = credential
        self._http = session or requests.Session()
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds

    # -- plumbing -----------------------------------------------------------

    def _auth_headers(self) -> dict:
        token = self._credential.get_token(GRAPH_SCOPE).token
        return {"Authorization": f"Bearer {token}"}

    def _send(self, method: str, url: str, *, authenticated: bool = True,
              ok: tuple[int, ...] = (200, 201), **kwargs) -> requests.Response:
        """Send a request, retrying throttling / transient errors with backoff."""
        extra_headers = kwargs.pop("headers", None) or {}
        for attempt in range(1, self._max_attempts + 1):
            headers = dict(extra_headers)
            if authenticated:
                headers.update(self._auth_headers())
            try:
                resp = self._http.request(method, url, headers=headers, timeout=300, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self._max_attempts:
                    raise
                self._sleep(attempt, None)
                continue
            if resp.status_code in ok:
                return resp
            if resp.status_code in RETRY_STATUS and attempt < self._max_attempts:
                self._sleep(attempt, resp.headers.get("Retry-After"))
                continue
            raise requests.HTTPError(
                f"{method} {_redact(url)} -> {resp.status_code}: {resp.text[:500]}", response=resp
            )
        raise AssertionError("unreachable")

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        try:
            delay = float(retry_after) if retry_after else self._backoff * 2 ** (attempt - 1)
        except ValueError:
            delay = self._backoff * 2 ** (attempt - 1)
        time.sleep(min(delay, 120))

    # -- lookups ------------------------------------------------------------

    def resolve_drive_id(self, site_hostname: str, site_path: str, library: str) -> str:
        """Find the drive id of a document library by its display name."""
        site_path = site_path.strip("/")
        site = self._send("GET", f"{GRAPH}/sites/{site_hostname}:/{quote(site_path)}").json()
        url = f"{GRAPH}/sites/{site['id']}/drives?$select=id,name"
        while url:
            page = self._send("GET", url).json()
            for drive in page.get("value", []):
                if drive["name"].lower() == library.lower():
                    return drive["id"]
            url = page.get("@odata.nextLink")
        raise LookupError(f"document library '{library}' not found on site '{site_path}'")

    def get_item(self, drive_id: str, path: str) -> dict | None:
        resp = self._send("GET", f"{_item_url(drive_id, path)}?$select=id,size,file",
                          ok=(200, 404))
        return None if resp.status_code == 404 else resp.json()

    # -- uploads ------------------------------------------------------------

    def upload_stream(self, drive_id: str, path: str, stream, size: int, *,
                      replace: bool = False, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict:
        """Upload exactly `size` bytes from `stream` to `path` in the drive.

        Returns {"id", "size", "sha256"}. The final chunk is only sent after the
        stream has been read to EOF, so a zip CRC failure (raised by zipfile on
        the last read) aborts the session and nothing is committed.
        """
        if chunk_size % CHUNK_UNIT or not 0 < chunk_size <= 60 * 1024 * 1024:
            raise ValueError("chunk_size must be a multiple of 320 KiB and at most 60 MiB")
        conflict = "replace" if replace else "fail"
        digest = hashlib.sha256()

        if size == 0:
            # Upload sessions cannot create empty files; use a simple upload.
            _expect_eof(stream)
            item = self._send(
                "PUT",
                f"{_item_url(drive_id, path)}/content?@microsoft.graph.conflictBehavior={conflict}",
                data=b"",
            ).json()
            return {"id": item["id"], "size": 0, "sha256": digest.hexdigest()}

        session = self._send(
            "POST",
            f"{_item_url(drive_id, path)}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
        ).json()
        upload_url = session["uploadUrl"]

        try:
            offset = 0
            item = None
            while offset < size:
                chunk = _read_exact(stream, min(chunk_size, size - offset))
                if not chunk:
                    raise ValueError(f"source ended after {offset} of {size} bytes")
                end = offset + len(chunk)
                if end == size:
                    _expect_eof(stream)  # triggers zipfile's CRC check before commit
                digest.update(chunk)
                # The upload URL is pre-authenticated; sending a bearer token to it fails.
                resp = self._send(
                    "PUT",
                    upload_url,
                    authenticated=False,
                    ok=(200, 201, 202),
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end - 1}/{size}",
                    },
                )
                offset = end
                if resp.status_code in (200, 201):
                    item = resp.json()
            if item is None:
                raise RuntimeError("upload session did not return the created item")
        except BaseException:
            self._cancel(upload_url)
            raise

        return {"id": item["id"], "size": item.get("size", size), "sha256": digest.hexdigest()}

    def _cancel(self, upload_url: str) -> None:
        try:
            self._http.delete(upload_url, timeout=30)
        except requests.RequestException:
            log.warning("could not cancel upload session")


def _item_url(drive_id: str, path: str) -> str:
    return f"{GRAPH}/drives/{drive_id}/root:/{quote(path.strip('/'), safe='/')}:"


def _redact(url: str) -> str:
    # Never log file paths (may contain PII) or upload URL query strings (they carry auth).
    parts = urlsplit(url)
    path = parts.path.split("/root:", 1)[0]
    return f"{parts.scheme}://{parts.netloc}{path}"


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        data = stream.read(n - len(buf))
        if not data:
            break
        buf += data
    return bytes(buf)


def _expect_eof(stream) -> None:
    if stream.read(1):
        raise ValueError("source is larger than its declared size")
