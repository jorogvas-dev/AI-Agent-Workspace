# Reproducible document-analysis TEST stack (v0.6e)

Specification only; the commands below are operator-run adoption instructions,
not evidence of deployment. Use [the new Compose file](docker-compose.document-analysis.yml)
alone, with project `agent-document-test`. Never merge it with the older test
Compose files. Keep the same PowerShell session through adoption/rollback.

Only `knowledge-service-test`, `planner-service-test`, and `open-webui-test`
are adopted. Existing `chromadb-test` is an external dependency on
`agent-internal`; no database container, database mount, or network is managed
here. Leave the knowledge candidate and all production containers untouched.

## 1. Preflight and local image snapshot

From `E:\AI-Agent-Workspace`, verify Docker Desktop is healthy, then:

- Verify existing networks `agent-internal` and `planner-egress-test` with
  `docker network inspect <name>`, and volume `open-webui-test-data` with
  `docker volume inspect open-webui-test-data`. Do not create replacements.
- Inspect only `.NetworkSettings.Networks`, `.State`, and `.Image` for the three
  original test containers and `chromadb-test`. Require Chroma on `agent-internal`
  and the running planner on `planner-egress-test`. Do not dump container
  environments. The historical planner Compose file still names built-in
  `bridge`, and the older knowledge Compose file names `agent-test`; these files
  do not prove current runtime attachments. Stop if prerequisites disagree.
- Confirm the new project has no existing containers:
  `docker ps -aq --filter label=com.docker.compose.project=agent-document-test`
  must be empty before this one-time adoption.
- Verify existing directories [input](input), [knowledge](knowledge),
  [planner-v05b](planner-v05b), and [UI exports](temp/ui-v06d), plus the existing
  files `test-token.txt`, `planner-api-token.txt`, and `openwebui-test-secret-key.txt`.
  Do not display secrets or create new credentials. Verify source files
  [server.py](../knowledge-service/server.py), [scripts](../scripts), and
  [the tool](../tools/analyze_with_planner.py) exist. Missing bind sources fail
  closed (`create_host_path: false`).
- Export/verify both model and tool JSON locally under `test-environment/temp/ui-v06d`
  before stopping WebUI. Do not publish exports or print secret-bearing valves.

These are **local image IDs**, not registry manifest digests. All three cached
images must exist, or be restored from a trusted offline Docker image archive.
This snapshot is not rebuildable from these IDs alone. Do not substitute a
floating image or use `image@sha256:...` for these local IDs.

Run guarded tagging; any existing alias pointing elsewhere aborts adoption:

```powershell
Set-Location 'E:\AI-Agent-Workspace'
$stackFile = Join-Path (Get-Location) 'test-environment/docker-compose.document-analysis.yml'
$snapshots = [ordered]@{
  'local-agent-test/knowledge:v06e' = 'sha256:7dbc132569c39f39cbaaa64720e7a3d916de3a890d99a9d02680d47e01c4b5b1'
  'local-agent-test/planner:v06e'   = 'sha256:880c6d21806d53e0b9b1083684c2493b9c155c762f2d956af62713c8f329318b'
  'local-agent-test/webui:v06e'     = 'sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd'
}
foreach ($entry in $snapshots.GetEnumerator()) {
  $cached = & docker image inspect --format '{{.Id}}' $entry.Value
  if ($LASTEXITCODE -ne 0 -or $cached -ne $entry.Value) { throw 'Required cached image unavailable; restore offline first.' }
  $tagged = & docker image inspect --format '{{.Id}}' $entry.Key 2>$null
  if ($LASTEXITCODE -eq 0) {
    if ($tagged -ne $entry.Value) { throw "Conflicting alias; refusing to retag $($entry.Key)" }
  } else {
    & docker image tag $entry.Value $entry.Key
    if ($LASTEXITCODE -ne 0) { throw 'Local image tagging failed.' }
  }
}
```

Require the originals' `.Image` values to match their respective snapshot IDs;
otherwise review the baseline instead of silently adopting different images.
Source binds preserve current server/scripts/tool code across recreation, so
retain the reviewed source checkout alongside the offline images.

## 2. Embedding/network gate (before stopping anything)

All new services join both existing external networks. This **deliberately moves
knowledge-service-test egress from Docker's built-in bridge to
planner-egress-test**. `planner-service` remains its Compose service DNS alias,
preserving the tool URLs `http://planner-service:8105/...`.

First require the current `planner-service-test` already to use
`planner-egress-test` (preflight above); do not attach networks automatically.
Run this bounded probe from that container. It must return exactly one finite
768-dimensional embedding. On failure, stop adoption; do not change LM Studio,
its model, or network security to bypass the check.

```powershell
$embeddingProbe = @'
import json, math, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("Embedding redirects are not permitted")
body = json.dumps({"model":"text-embedding-nomic-embed-text-v1.5@q8_0","input":["isolated connectivity probe"]}).encode()
request = urllib.request.Request("http://host.docker.internal:1234/v1/embeddings", data=body, headers={"Content-Type":"application/json"})
with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
    raw = response.read(1000001)
assert len(raw) <= 1000000
data = json.loads(raw)["data"]
assert len(data) == 1 and type(data[0]["index"]) is int and data[0]["index"] == 0
vector = data[0]["embedding"]
assert isinstance(vector, list) and len(vector) == 768
assert all(type(value) in (int, float) and math.isfinite(value) for value in vector)
print("Embedding connectivity passed: one finite 768-dimensional vector")
'@
$embeddingProbe | & docker exec -i planner-service-test python -
if ($LASTEXITCODE -ne 0) { throw 'Embedding connectivity gate failed; do not cut over.' }
```

## 3. Scoped credentials and quiet Compose checks

Use an explicit empty temporary env file to suppress automatic `.env` loading.
The helper reads the existing test knowledge token only during a Compose command,
never prints it, and restores the previous process environment in `finally`.
Keep this session open. Do not run plain `compose config`, environment dumps, or
container inspections that include credentials.

```powershell
$emptyEnvFile = New-TemporaryFile
function Invoke-DocumentCompose {
  param([string[]]$ComposeArgs)
  $previous = [Environment]::GetEnvironmentVariable('TEST_KNOWLEDGE_TOKEN', 'Process')
  try {
    $tokenPath = Join-Path (Get-Location) 'test-environment/test-token.txt'
    $env:TEST_KNOWLEDGE_TOKEN = [IO.File]::ReadAllText($tokenPath).Trim()
    if ([string]::IsNullOrWhiteSpace($env:TEST_KNOWLEDGE_TOKEN)) { throw 'Existing test token is empty.' }
    & docker compose --project-name agent-document-test --env-file $emptyEnvFile.FullName -f $stackFile @ComposeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Document TEST Compose command failed.' }
  } finally {
    [Environment]::SetEnvironmentVariable('TEST_KNOWLEDGE_TOKEN', $previous, 'Process')
  }
}
Invoke-DocumentCompose -ComposeArgs @('config', '--quiet')
```

For credential-free syntax validation, use a separate validation subprocess with
`TEST_KNOWLEDGE_TOKEN` set to a synthetic value and an explicit empty temporary
env file; invoke only `config --quiet`. That checks syntax, not network/resource
availability or readiness. Never use that synthetic value to start containers.

## 4. One-time adoption

Proceed only after all gates and UI exports pass. Generate unique backup names,
record this map and original IDs, and refuse existing backup names. None of the
commands below operate on Chroma, the knowledge candidate, or production.

```powershell
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$backups = [ordered]@{}
$originalIds = @{}
foreach ($name in @('knowledge-service-test', 'planner-service-test', 'open-webui-test')) {
  $originalIds[$name] = & docker container inspect --format '{{.Id}}' $name
  if ($LASTEXITCODE -ne 0) { throw "Original test container missing: $name" }
  $backup = "$name-v06e-backup-$stamp"
  & docker container inspect --format '{{.Name}}' $backup 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw 'Backup name already exists; never overwrite it.' }
  $backups[$name] = $backup
}
$backups  # Record these non-secret names before proceeding.
& docker stop knowledge-service-test planner-service-test open-webui-test
if ($LASTEXITCODE -ne 0) { throw 'Stop failed; inspect before continuing.' }
foreach ($name in $backups.Keys) {
  & docker rename $name $backups[$name]
  if ($LASTEXITCODE -ne 0) { throw 'Rename failed; restore already-renamed originals before retrying.' }
}
Invoke-DocumentCompose -ComposeArgs @('up', '-d', '--no-build', '--pull', 'never')
```

No builds or pulls are permitted. Do not run the old Compose definitions against
the renamed backups. Preserve backups until acceptance; this runbook does not
delete them. Only the test WebUI publishes a port: `127.0.0.1:3001:8080`.

## 5. Readiness and acceptance

- Check all three new containers are running, WebUI is healthy, and
  `http://127.0.0.1:3001` loads. Review bounded logs for startup failures, without
  dumping environments. A running process alone is not readiness.
- Repeat the embedding probe after cutover. From planner, require
  `http://knowledge-service-test:8002/health` to report readiness and the expected
  indexed test data; require `http://planner-service:8105/health` from WebUI.
  Verify `host.docker.internal:1234/v1/models` is reachable from WebUI.
- Authenticated catalog smoke from planner (prints counts only):

  ```powershell
  $catalogProbe = @'
  from pathlib import Path
  from scripts.knowledge_adapter_v04c import create_isolated_adapter
  page = create_isolated_adapter(Path("/run/secrets/agent_knowledge_token")).list_documents(limit=50, offset=0)
  assert page["status"] == "success"
  print("Authenticated isolated catalog OK; entries:", page["result_count"])
  '@
  $catalogProbe | & docker exec -i planner-service-test python -
  if ($LASTEXITCODE -ne 0) { throw 'Authenticated catalog check failed.' }
  ```

- From WebUI, import the mounted `/workspace/tools/analyze_with_planner.py`
  using Python's `importlib.util`, call `Tools().analyze_document()` with the
  previously passed explicit input/knowledge document requests, and parse its
  JSON. This exercises the authenticated fixed `/analyze-document` route. Require
  original fields/quotes, `selected_document`, expected `success/status`, and
  `execution_performed=false`; print only a status summary. An unauthenticated
  POST to that route must return 401. COMMIT/reconcile routes must return 404.
  These are smoke checks to perform, not checks executed while writing this spec.

### Working UI configuration

Use preset **qWEN** (ID **qwen**), based on the existing Qwen model, with native
function calling. Attach and enable the custom planner tool; disable the builtin
**Knowledge Base** category on that preset. Verify the tool source actually
saved in WebUI includes `analyze_document`: the read-only source mount alone does
not install/update WebUI's saved tool. If necessary import/review the local tool
export; do not copy production settings or credentials.

Model/tool JSON exports live under [temp/ui-v06d](temp/ui-v06d). Prior live UI
cases were reported passed (not re-run for this specification):

1. Explicit `input` document: requested facts and source evidence returned.
2. Explicit `knowledge` document: requested facts and source evidence returned.
3. Ambiguous bare filename: bounded document choices, with zero pipeline calls.

Repeat all three through qWEN after cutover. Generic analysis is extraction-only,
with structural evidence validation, not a semantic-correctness guarantee. Legacy
job-offer calculations remain the separate `analyze_with_planner` tool function.
No generic writes, approvals, COMMIT, or reconcile are exposed.

## 6. Rollback (only the three new containers)

If adoption partially fails, do not rerun renames blindly. Use the recorded
`$backups`/`$originalIds` to identify what moved. After a completed rename phase,
the following guarded rollback removes only new-project containers, never volumes
or networks, then restores and starts the originals:

```powershell
foreach ($name in $backups.Keys) {
  $id = & docker container inspect --format '{{.Id}}' $name 2>$null
  if ($LASTEXITCODE -eq 0) {
    $labelsJson = & docker container inspect --format '{{json .Config.Labels}}' $name
    if ($LASTEXITCODE -ne 0) { throw 'Refusing removal: label inspection failed.' }
    try {
      $labels = ConvertFrom-Json -InputObject $labelsJson -ErrorAction Stop
    } catch {
      throw 'Refusing removal: invalid container labels.'
    }
    if ($null -eq $labels -or $labels -isnot [pscustomobject] -or
        $labelsJson -isnot [string] -or -not $labelsJson.TrimStart().StartsWith('{')) {
      throw 'Refusing removal: invalid container labels.'
    }
    $project = $labels.'com.docker.compose.project'
    if ($project -isnot [string] -or $project -cne 'agent-document-test' -or $id -eq $originalIds[$name]) {
      throw 'Refusing removal: container is not one of the new TEST stack containers.'
    }
    & docker container rm --force $name
    if ($LASTEXITCODE -ne 0) { throw 'New TEST container removal failed.' }
  }
}
foreach ($name in $backups.Keys) {
  $id = & docker container inspect --format '{{.Id}}' $backups[$name]
  if ($LASTEXITCODE -ne 0 -or $id -ne $originalIds[$name]) { throw 'Original backup identity mismatch.' }
  & docker container inspect --format '{{.Id}}' $name 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw 'Original name is occupied; never overwrite it.' }
  & docker rename $backups[$name] $name
  if ($LASTEXITCODE -ne 0) { throw 'Original name restoration failed.' }
}
& docker start knowledge-service-test planner-service-test open-webui-test
if ($LASTEXITCODE -ne 0) { throw 'Original TEST container startup failed.' }
```

The shared external WebUI volume and external Chroma retain their state. Renamed
containers are not data backups; rollback restores container configuration, not
UI/database history. Test watcher indexing may change the isolated index during
adoption. Recheck original readiness and saved UI configuration after rollback.
Finally remove only the empty scratch env file with
`Remove-Item -LiteralPath $emptyEnvFile.FullName`; never remove volumes/networks
or prune resources. All Compose invocations restore the caller's token environment.
