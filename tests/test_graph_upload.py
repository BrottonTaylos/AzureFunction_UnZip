import io
import zipfile

import pytest

from shared.graph_upload import CHUNK_UNIT, GraphClient, _redact
from tests.fakes import FakeCredential, FakeGraphSession


def client(session):
    return GraphClient(FakeCredential(), session=session, backoff_seconds=0)


def test_chunked_upload_reassembles_file_and_omits_auth_on_upload_url():
    payload = bytes(range(256)) * 5000  # 1.28 MB -> 4 chunks of 320 KiB
    session = FakeGraphSession()

    result = client(session).upload_stream("drive", "folder/f.bin", io.BytesIO(payload),
                                           len(payload), chunk_size=CHUNK_UNIT)

    assert bytes(session.uploaded) == payload
    assert result["id"] == "item-1"
    chunk_calls = [c for c in session.calls if c[1].startswith("https://upload.example/")]
    assert len(chunk_calls) == 4
    assert all("Authorization" not in headers for _, _, headers in chunk_calls)
    create = session.calls[0]
    assert create[1].endswith("/drives/drive/root:/folder/f.bin:/createUploadSession")
    assert create[2]["Authorization"] == "Bearer token"


def test_throttled_chunk_is_retried_with_same_range():
    payload = b"x" * (CHUNK_UNIT + 10)
    session = FakeGraphSession(fail_first_chunk_with=429)
    client(session).upload_stream("drive", "f.bin", io.BytesIO(payload), len(payload),
                                  chunk_size=CHUNK_UNIT)
    assert bytes(session.uploaded) == payload


def test_size_mismatch_aborts_before_final_chunk_and_cancels_session():
    session = FakeGraphSession()
    with pytest.raises(ValueError, match="larger than its declared size"):
        client(session).upload_stream("drive", "f.bin", io.BytesIO(b"abcdef"), 5)
    assert session.uploaded == b""
    assert session.cancelled


def test_corrupt_zip_entry_is_never_committed():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("f.txt", b"A" * 1000)
    data = bytearray(buf.getvalue())
    data[data.find(b"A" * 1000) + 500] = ord("B")  # flip a byte -> CRC mismatch
    zf = zipfile.ZipFile(io.BytesIO(bytes(data)))
    info = zf.getinfo("f.txt")
    session = FakeGraphSession()

    with zf.open(info) as src, pytest.raises(zipfile.BadZipFile):
        client(session).upload_stream("drive", "f.txt", src, info.file_size)
    assert session.uploaded == b""
    assert session.cancelled


def test_empty_file_uses_simple_upload():
    session = FakeGraphSession()
    result = client(session).upload_stream("drive", "empty.txt", io.BytesIO(b""), 0)
    assert result["id"] == "item-empty"
    assert not any(c[1].endswith("createUploadSession") for c in session.calls)


def test_paths_are_url_encoded():
    session = FakeGraphSession()
    client(session).upload_stream("drive", "a b/#1 50%.txt", io.BytesIO(b"x"), 1)
    assert "/root:/a%20b/%231%2050%25.txt:/createUploadSession" in session.calls[0][1]


def test_invalid_chunk_size_rejected():
    with pytest.raises(ValueError):
        client(FakeGraphSession()).upload_stream("d", "f", io.BytesIO(b"x"), 1, chunk_size=1000)


def test_redact_hides_paths_and_upload_tokens():
    assert _redact("https://graph.microsoft.com/v1.0/drives/d/root:/Jane Doe.pdf:/content") \
        == "https://graph.microsoft.com/v1.0/drives/d"
    assert "tempauth" not in _redact("https://upload.example/session?tempauth=secret")
