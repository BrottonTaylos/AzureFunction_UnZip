import io
import zipfile

import function_app
from shared.graph_upload import GraphClient
from tests.fakes import FakeCredential, FakeGraphSession, FakeResponse

JOB = {"container": "c", "blob": "b.zip", "targetFolder": "Import", "replace": False, "maxParallel": 2}


class ExistingItemSession(FakeGraphSession):
    def __init__(self, existing_size=None):
        super().__init__()
        self.existing_size = existing_size

    def request(self, method, url, **kwargs):
        if method == "GET" and "?$select=id,size,file" in url:
            self.calls.append((method, url, {}))
            if self.existing_size is None:
                return FakeResponse(404)
            return FakeResponse(200, {"id": "x", "size": self.existing_size})
        return super().request(method, url, **kwargs)


def one_entry_zip(data=b"hello"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("f.txt", data)
    zf = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    return zf, zf.infolist()[0]


def run_entry(session, job=JOB):
    zf, info = one_entry_zip()
    graph = GraphClient(FakeCredential(), session=session, backoff_seconds=0)
    entry = {"index": 0, "target": "f.txt", "size": info.file_size}
    return function_app._transfer_entry(zf, info, entry, graph, "drive", job)


def test_entry_uploaded_into_target_folder():
    session = ExistingItemSession()
    result = run_entry(session)
    assert result["status"] == "uploaded" and result["target"] == "Import/f.txt"
    assert bytes(session.uploaded) == b"hello"


def test_existing_same_size_is_skipped():
    session = ExistingItemSession(existing_size=5)
    assert run_entry(session)["status"] == "skipped"
    assert session.uploaded == b""


def test_existing_different_size_fails_unless_replace():
    result = run_entry(ExistingItemSession(existing_size=99))
    assert result["status"] == "failed" and "TargetExistsError" in result["error"]
    session = ExistingItemSession(existing_size=99)
    assert run_entry(session, {**JOB, "replace": True})["status"] == "uploaded"


class FakeContext:
    def __init__(self, job):
        self._job = job
        self.status = None

    def get_input(self):
        return self._job

    def call_activity(self, name, payload):
        return (name, payload)

    def task_all(self, tasks):
        return tasks

    def set_custom_status(self, status):
        self.status = status


def test_orchestrator_fans_out_in_batches_and_summarises():
    ctx = FakeContext(JOB)
    gen = function_app.orchestrate(ctx)

    name, _ = next(gen)
    assert name == "prepare_transfer"
    groups = [[{"index": i}] for i in range(3)]
    tasks = gen.send({"driveId": "d", "groups": groups, "totalFiles": 3, "totalBytes": 30})
    assert [t[1]["entries"] for t in tasks] == groups[:2]  # maxParallel = 2
    tasks = gen.send([[{"index": 0, "status": "uploaded"}], [{"index": 1, "status": "skipped"}]])
    assert [t[1]["entries"] for t in tasks] == groups[2:]
    try:
        gen.send([[{"index": 2, "status": "failed", "error": "x"}]])
    except StopIteration as stop:
        summary = stop.value
    assert summary["outcome"] == "completed_with_failures"
    assert (summary["uploaded"], summary["skipped"], summary["failed"]) == (1, 1, 1)
    assert ctx.status == {"filesProcessed": 3, "filesTotal": 3, "failed": 1}
