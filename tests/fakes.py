"""In-memory stand-ins for the Blob SDK and the Graph HTTP endpoint."""

from types import SimpleNamespace


class FakeBlobClient:
    def __init__(self, data: bytes):
        self.data = data
        self.etag = '"0x1"'
        self.range_requests = []

    def get_blob_properties(self):
        return SimpleNamespace(size=len(self.data), etag=self.etag)

    def download_blob(self, offset, length, etag, match_condition, max_concurrency):
        assert etag == self.etag, "reads must be pinned to the opened ETag"
        self.range_requests.append((offset, length))
        chunk = self.data[offset:offset + length]
        return SimpleNamespace(readall=lambda: chunk)


class FakeCredential:
    def get_token(self, *scopes):
        return SimpleNamespace(token="token")


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeGraphSession:
    """Accepts upload-session chunks and assembles the uploaded file."""

    def __init__(self, fail_first_chunk_with=None):
        self.calls = []
        self.uploaded = bytearray()
        self.cancelled = False
        self._fail_first = fail_first_chunk_with

    def request(self, method, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        if url.endswith("/createUploadSession"):
            return FakeResponse(200, {"uploadUrl": "https://upload.example/session?tempauth=secret"})
        if url.endswith("/content?@microsoft.graph.conflictBehavior=fail"):
            return FakeResponse(201, {"id": "item-empty", "size": 0})
        if url.startswith("https://upload.example/"):
            if self._fail_first is not None:
                status, self._fail_first = self._fail_first, None
                return FakeResponse(status, headers={"Retry-After": "0"})
            start_end, total = headers["Content-Range"].removeprefix("bytes ").split("/")
            start, end = (int(x) for x in start_end.split("-"))
            assert start == len(self.uploaded), "chunks must arrive in order"
            assert len(data) == end - start + 1
            self.uploaded += data
            if len(self.uploaded) == int(total):
                return FakeResponse(201, {"id": "item-1", "size": int(total)})
            return FakeResponse(202, {"nextExpectedRanges": [f"{end + 1}-"]})
        raise AssertionError(f"unexpected request {method} {url}")

    def delete(self, url, timeout=None):
        self.cancelled = True
