"""One-off transfer: unzip a large archive in Blob Storage into a SharePoint library.

POST /api/unzip  {"container": "...", "blob": "...", "targetFolder": "...", "replace": false}
starts a Durable Functions orchestration:

  prepare_transfer  - opens the zip via range reads, validates every entry,
                      resolves the SharePoint drive and splits the work into groups
  transfer_group    - (fan-out) streams each entry of a group into SharePoint

Re-running the same request is safe: files already in SharePoint with the same
size are skipped, so a run with failures can simply be started again.

Logging deliberately records only entry indexes, sizes and error types - never
file names or contents, which may contain PII.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import time

import azure.durable_functions as df
import azure.functions as func
import requests
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from shared.graph_upload import GraphClient, TargetExistsError
from shared.zip_stream import (
    Limits,
    ZipValidationError,
    open_blob_zip,
    plan_entries,
    sharepoint_relative_path,
)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)
log = logging.getLogger("unzip")

# Managed Identity in Azure (set AZURE_CLIENT_ID for a user-assigned identity);
# falls back to your Azure CLI login when running locally.
CREDENTIAL = DefaultAzureCredential(exclude_interactive_browser_credential=True)

# Work is grouped so each activity opens the zip (and reads its central
# directory) once for many entries, rather than once per entry.
GROUP_MAX_FILES = 500
GROUP_MAX_BYTES = 5 * 1024**3
ENTRY_ATTEMPTS = 3


def _setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"app setting {name} is not configured")
    return value


def _zip_password() -> bytes | None:
    password = os.environ.get("ZIP_PASSWORD")
    return password.encode("utf-8") if password else None


def _blob_client(job: dict):
    service = BlobServiceClient(_setting("SOURCE_BLOB_ACCOUNT_URL"), credential=CREDENTIAL)
    return service.get_blob_client(job["container"], job["blob"])


def _bad_request(message: str) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"error": message}), status_code=400,
                             mimetype="application/json")


@app.route(route="unzip", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_unzip(req: func.HttpRequest, client) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _bad_request("request body must be JSON")

    container, blob = body.get("container"), body.get("blob")
    if not isinstance(container, str) or not container or not isinstance(blob, str) or not blob:
        return _bad_request("'container' and 'blob' are required")
    target_folder = body.get("targetFolder") or ""
    try:
        target_folder = sharepoint_relative_path(target_folder) if target_folder else ""
    except ZipValidationError as exc:
        return _bad_request(f"invalid targetFolder: {exc}")

    job = {
        "container": container,
        "blob": blob,
        "targetFolder": target_folder,
        "replace": bool(body.get("replace", False)),
        "maxParallel": max(1, int(_setting("MAX_PARALLEL_GROUPS", "8"))),
    }
    instance_id = await client.start_new("unzip_orchestrator", client_input=job)
    log.info("started unzip orchestration %s", instance_id)
    return client.create_check_status_response(req, instance_id)


@app.orchestration_trigger(context_name="context")
def unzip_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from orchestrate(context))


def orchestrate(context: df.DurableOrchestrationContext):
    job = context.get_input()
    plan = yield context.call_activity("prepare_transfer", job)
    groups = plan["groups"]

    results: list[dict] = []
    batch = job["maxParallel"]
    for start in range(0, len(groups), batch):
        tasks = [
            context.call_activity("transfer_group", {"job": job, "driveId": plan["driveId"], "entries": g})
            for g in groups[start:start + batch]
        ]
        for group_result in (yield context.task_all(tasks)):
            results.extend(group_result)
        context.set_custom_status({
            "filesProcessed": len(results),
            "filesTotal": plan["totalFiles"],
            "failed": sum(r["status"] == "failed" for r in results),
        })

    failures = [r for r in results if r["status"] == "failed"]
    return {
        "outcome": "completed_with_failures" if failures else "completed",
        "filesTotal": plan["totalFiles"],
        "bytesTotal": plan["totalBytes"],
        "uploaded": sum(r["status"] == "uploaded" for r in results),
        "skipped": sum(r["status"] == "skipped" for r in results),
        "failed": len(failures),
        "failures": failures,
        "manifest": results,
    }


@app.activity_trigger(input_name="job")
def prepare_transfer(job: dict) -> dict:
    limits = Limits(
        max_entries=int(_setting("ZIP_MAX_ENTRIES", str(Limits.max_entries))),
        max_total_uncompressed=int(_setting("ZIP_MAX_TOTAL_BYTES", str(Limits.max_total_uncompressed))),
        max_ratio=int(_setting("ZIP_MAX_RATIO", str(Limits.max_ratio))),
    )
    zf = open_blob_zip(_blob_client(job))
    entries = plan_entries(zf, limits, has_password=_zip_password() is not None,
                           target_folder=job["targetFolder"])

    drive_id = GraphClient(CREDENTIAL).resolve_drive_id(
        _setting("SHAREPOINT_SITE_HOSTNAME"),
        _setting("SHAREPOINT_SITE_PATH"),
        _setting("SHAREPOINT_LIBRARY"),
    )

    groups: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = 0
    for entry in entries:
        if current and (len(current) >= GROUP_MAX_FILES or current_bytes + entry["size"] > GROUP_MAX_BYTES):
            groups.append(current)
            current, current_bytes = [], 0
        current.append(entry)
        current_bytes += entry["size"]
    if current:
        groups.append(current)

    total_bytes = sum(e["size"] for e in entries)
    log.info("validated %d files (%d bytes) in %d groups", len(entries), total_bytes, len(groups))
    return {"driveId": drive_id, "groups": groups, "totalFiles": len(entries), "totalBytes": total_bytes}


@app.activity_trigger(input_name="work")
def transfer_group(work: dict) -> list[dict]:
    job, drive_id = work["job"], work["driveId"]
    graph = GraphClient(CREDENTIAL)
    zf = open_blob_zip(_blob_client(job))
    infos = zf.infolist()
    return [_transfer_entry(zf, infos[e["index"]], e, graph, drive_id, job) for e in work["entries"]]


def _transfer_entry(zf, info, entry: dict, graph: GraphClient, drive_id: str, job: dict) -> dict:
    target = posixpath.join(job["targetFolder"], entry["target"]) if job["targetFolder"] else entry["target"]
    result = {"index": entry["index"], "target": target, "size": entry["size"]}

    for attempt in range(1, ENTRY_ATTEMPTS + 1):
        try:
            existing = graph.get_item(drive_id, target)
            if existing is not None and not job["replace"]:
                if existing.get("size") == entry["size"]:
                    log.info("entry #%d already present, skipped", entry["index"])
                    return {**result, "status": "skipped"}
                raise TargetExistsError("a different file already exists at the target path")
            with zf.open(info, pwd=_zip_password()) as src:
                uploaded = graph.upload_stream(drive_id, target, src, info.file_size,
                                               replace=job["replace"])
            log.info("entry #%d uploaded (%d bytes)", entry["index"], entry["size"])
            return {**result, "status": "uploaded", "sha256": uploaded["sha256"]}
        except (requests.RequestException, AzureError) as exc:
            log.warning("entry #%d attempt %d failed: %s", entry["index"], attempt, type(exc).__name__)
            if attempt == ENTRY_ATTEMPTS:
                return {**result, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            time.sleep(15 * attempt)
        except Exception as exc:  # not retryable: bad CRC, conflict, unsupported entry...
            log.warning("entry #%d failed: %s", entry["index"], type(exc).__name__)
            return {**result, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    raise AssertionError("unreachable")
