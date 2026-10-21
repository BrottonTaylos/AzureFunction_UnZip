# Unzip from Blob Storage to SharePoint (one-off, PII-safe)

An Azure Function (Python, Durable Functions) that takes a large zip sitting in
Blob Storage, extracts it **in a stream**, and uploads every file into a
SharePoint Online document library through Microsoft Graph.

- The zip is never downloaded or written to disk. It is read with HTTP range
  requests and each entry is streamed straight into a Graph upload session in 10 MiB chunks.
- Every connection uses the Function's **Managed Identity**. There are no secrets,
  connection strings or client secrets.
- SharePoint access is limited to **one site** (`Sites.Selected`), not the whole tenant.
- The whole archive is checked before anything is uploaded: path traversal,
  zip-bomb ratios, size and entry limits, unsupported compression, encryption,
  and name collisions.
- A zip CRC failure is detected before the last chunk is sent, so a corrupt file
  is never written to SharePoint.
- Logs record only entry numbers, sizes and error types, never file names or
  contents.
- You can safely run it again: files that are already in SharePoint with the same size are skipped.

## Why an Azure Function (and not Power Automate / ADF)

| Option | Verdict |
|---|---|
| Power Automate | Connector payload limits (~100 MB) and archive-size limits on "Extract archive"; file contents pass through connector run history. Not suitable for a massive PII file. |
| ADF | Can unzip blob → blob (Binary dataset + ZipDeflate), but has **no SharePoint Online file sink**, so it can't do the SharePoint leg. |
| **Azure Function** | Does both legs, streams, uses only Managed Identity and keeps PII inside your Azure boundary until it reaches SharePoint over TLS. |

## How it works

```
POST /api/unzip ─▶ unzip_orchestrator
                    ├─ prepare_transfer   open zip via range reads, validate all entries,
                    │                     resolve SharePoint drive, split into groups
                    └─ transfer_group ×N  (fan-out, MAX_PARALLEL_GROUPS at a time)
                         └─ per entry: skip-if-present → stream → Graph upload session
```

| File | Purpose |
|---|---|
| `function_app.py` | HTTP starter, orchestrator, activities |
| `shared/zip_stream.py` | Seekable blob reader (ETag-pinned), entry validation and SharePoint name mapping |
| `shared/graph_upload.py` | Graph client: drive lookup, chunked upload sessions, retry/backoff |
| `tests/` | Unit tests (no Azure needed): `pip install -r requirements-dev.txt && pytest` |

## Runbook

Replace the `<...>` placeholders. You need Azure CLI, Azure Functions Core Tools v4,
and PnP PowerShell, or another way to call Graph as a SharePoint admin.

### 1. Create the Function App (Flex Consumption, system-assigned identity)

```bash
RG=<resource-group>; LOC=<region>; APP=<function-app-name>; HOST_SA=<new-host-storage-account>
SRC_SA=<storage-account-holding-the-zip>

az storage account create -g $RG -n $HOST_SA -l $LOC --sku Standard_LRS \
  --min-tls-version TLS1_2 --allow-blob-public-access false --allow-shared-key-access false

az functionapp create -g $RG -n $APP --storage-account $HOST_SA \
  --flexconsumption-location $LOC --runtime python --runtime-version 3.11 \
  --instance-memory 2048 --deployment-storage-auth-type SystemAssignedIdentity

az functionapp identity assign -g $RG -n $APP
MI_OBJECT_ID=$(az functionapp identity show -g $RG -n $APP --query principalId -o tsv)
MI_APP_ID=$(az ad sp show --id $MI_OBJECT_ID --query appId -o tsv)
```

Lock the app down: HTTPS only, no FTP or basic-auth publishing.

```bash
az functionapp update -g $RG -n $APP --set httpsOnly=true
az resource update -g $RG --resource-type Microsoft.Web/sites/basicPublishingCredentialsPolicies \
  --parent sites/$APP -n scm --set properties.allow=false
az resource update -g $RG --resource-type Microsoft.Web/sites/basicPublishingCredentialsPolicies \
  --parent sites/$APP -n ftp --set properties.allow=false
```

If the source storage account uses a private endpoint (recommended), add VNet
integration to the Function App (`az functionapp vnet-integration add`) with a
subnet that can reach that private endpoint.

### 2. Storage permissions (RBAC, no keys)

```bash
SRC_ID=$(az storage account show -g $RG -n $SRC_SA --query id -o tsv)
HOST_ID=$(az storage account show -g $RG -n $HOST_SA --query id -o tsv)

# Read-only on the zip. Scope it to the container if you can.
az role assignment create --assignee-object-id $MI_OBJECT_ID --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" --scope "$SRC_ID/blobServices/default/containers/<container>"

# The Durable Functions task hub lives in the host storage account.
for ROLE in "Storage Blob Data Owner" "Storage Queue Data Contributor" "Storage Table Data Contributor"; do
  az role assignment create --assignee-object-id $MI_OBJECT_ID --assignee-principal-type ServicePrincipal \
    --role "$ROLE" --scope $HOST_ID
done
```

### 3. SharePoint permission: this site only

Create a **dedicated document library** first. Break inheritance on it, give
access only to the people who need the data, and apply your sensitivity
label or DLP policy at the library level.

a) Grant the identity the Graph application permission `Sites.Selected`. This needs
an admin who can grant app roles, such as a Privileged Role Administrator:

```bash
GRAPH_SP=$(az ad sp list --filter "appId eq '00000003-0000-0000-c000-000000000000'" --query "[0].id" -o tsv)
ROLE_ID=$(az ad sp show --id $GRAPH_SP --query "appRoles[?value=='Sites.Selected'].id" -o tsv)
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$MI_OBJECT_ID/appRoleAssignments" \
  --body "{\"principalId\":\"$MI_OBJECT_ID\",\"resourceId\":\"$GRAPH_SP\",\"appRoleId\":\"$ROLE_ID\"}"
```

b) Give that identity `write` on **only** the target site. A SharePoint admin runs this:

```powershell
Connect-PnPOnline -Url https://<tenant>.sharepoint.com/sites/<site> -Interactive -ClientId <your-pnp-app-id>
Grant-PnPAzureADAppSitePermission -AppId <MI_APP_ID> -DisplayName "unzip-func" `
  -Site https://<tenant>.sharepoint.com/sites/<site> -Permissions Write
```

### 4. App settings and deploy

```bash
# Host storage via the identity instead of a key-based connection string.
az functionapp config appsettings delete -g $RG -n $APP --setting-names AzureWebJobsStorage
az functionapp config appsettings set -g $RG -n $APP --settings \
  AzureWebJobsStorage__accountName=$HOST_SA \
  SOURCE_BLOB_ACCOUNT_URL=https://$SRC_SA.blob.core.windows.net \
  SHAREPOINT_SITE_HOSTNAME=<tenant>.sharepoint.com \
  SHAREPOINT_SITE_PATH=sites/<site> \
  SHAREPOINT_LIBRARY="<library display name>" \
  MAX_PARALLEL_GROUPS=8

func azure functionapp publish $APP --python
```

| Setting | Default | Meaning |
|---|---|---|
| `SOURCE_BLOB_ACCOUNT_URL` | required | Blob endpoint of the account holding the zip |
| `SHAREPOINT_SITE_HOSTNAME` / `SHAREPOINT_SITE_PATH` / `SHAREPOINT_LIBRARY` | required | The fixed destination. Callers can't redirect files elsewhere. |
| `MAX_PARALLEL_GROUPS` | 8 | Groups (≤500 files / ≤5 GiB each) transferred concurrently |
| `ZIP_MAX_ENTRIES` | 200000 | Zip-bomb guard |
| `ZIP_MAX_TOTAL_BYTES` | 2 TiB | Zip-bomb guard (total uncompressed) |
| `ZIP_MAX_RATIO` | 1000 | Rejects entries > 1 MiB that expand more than this ratio |
| `ZIP_PASSWORD` | unset | Only for ZipCrypto-protected archives. Use a Key Vault reference, `@Microsoft.KeyVault(SecretUri=...)`, never a plain value. |

### 5. Run it

```bash
KEY=$(az functionapp function keys list -g $RG -n $APP --function-name start_unzip --query default -o tsv)
curl -sS -X POST "https://$APP.azurewebsites.net/api/unzip?code=$KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"<container>","blob":"<path/to/file.zip>","targetFolder":"Import-2026-10","replace":false}'
```

The response includes `statusQueryGetUri`. Poll it to watch `customStatus`
(`filesProcessed` / `filesTotal` / `failed`). When it finishes, `output` has:

- `outcome`: `completed` or `completed_with_failures`
- `uploaded`, `skipped`, `failed`, `filesTotal`, `bytesTotal`
- `failures`: the target path and error for each failed file
- `manifest`: every file with its target path, size and SHA-256 of the extracted bytes

If the archive fails validation, the orchestration fails straight away with
the list of problems and nothing is uploaded. If some files fail, **send the
same request again**: files that already completed are skipped.

> The status URL and the `output` include file names. Treat the status URL like
> a secret and don't paste it into tickets or chat.

### 6. Verify

- `filesTotal` = `uploaded` + `skipped` and `failed` = 0.
- Spot-check in SharePoint: file count and total size of `targetFolder`
  (Library settings → Storage Metrics) match `filesTotal` / `bytesTotal`.
- Check that the identity can't reach other sites. A Graph call from the
  Function to any other site should return 403.
- Check that Application Insights has no file names or contents. Search traces for a known file name.

### 7. Tear down (one-off)

```powershell
Revoke-PnPAzureADAppSitePermission -PermissionId <id from Get-PnPAzureADAppSitePermission> `
  -Site https://<tenant>.sharepoint.com/sites/<site>
```

```bash
az functionapp delete -g $RG -n $APP            # also removes the system-assigned identity
az storage account delete -g $RG -n $HOST_SA     # task hub holds the manifest (file names)
# After the business confirms the SharePoint copy, delete the source zip
# (soft delete keeps it for the account's retention period).
az storage blob delete --account-name $SRC_SA -c <container> -n <path/to/file.zip> --auth-mode login
```

## Limitations

- **Deflate64**: Windows Explorer uses it for large files. **AES-encrypted** zips,
  the 7-Zip default for passwords, aren't supported by Python's `zipfile` either.
  Both are reported during validation. Ask the sender to re-zip with standard
  Deflate (`zip`, or 7-Zip with *Deflate* and *ZipCrypto*), or extract
  those entries another way.
- Empty folders in the zip aren't created. Folders are created by the files placed in them.
- SharePoint limits: 250 GB per file and 400 characters per path. Characters SharePoint
  rejects (`" * : < > ? \ |`) become `_`. Collisions this causes are reported during validation.
- If the source blob changes during the run, the run fails, because reads are pinned to its ETag.
- `functionTimeout` is 12 hours to cover very large single entries. If an
  instance is recycled mid-activity, Durable Functions retries the activity,
  and files that already completed are skipped.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
cp local.settings.sample.json local.settings.json   # fill in values; uses your `az login`
func start                                          # needs Azurite for AzureWebJobsStorage
```

Test only with synthetic data, never the real PII archive.
