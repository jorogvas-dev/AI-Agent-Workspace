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
- Inspect only identity, restart policy, Compose labels, `.NetworkSettings.Networks`,
  `.State`, and `.Image` metadata for the three
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

Before **any adoption mutation, including image tagging**, capture all three
original IDs and exact restart policies. Save the printed, non-secret recovery
record outside this session as well; do not replace captured policies with
assumptions from Compose. A confirmed incident involved a renamed backup planner
restarting with its retained `planner-service` network alias, so WebUI could reach
either API. Renaming does not remove network aliases or disable restart.

The helpers below inspect selected metadata only. All container mutations use
verified IDs. Keep this session open and do not run competing stack operations.
Unsupported policy metadata fails before mutation instead of losing retry counts.
Run guarded tagging only after capture; an existing image alias pointing elsewhere
aborts adoption:

```powershell
Set-Location 'E:\AI-Agent-Workspace'
$stackFile = Join-Path (Get-Location) 'test-environment/docker-compose.document-analysis.yml'
$snapshots = [ordered]@{
  'local-agent-test/knowledge:v06e' = 'sha256:7dbc132569c39f39cbaaa64720e7a3d916de3a890d99a9d02680d47e01c4b5b1'
  'local-agent-test/planner:v06e'   = 'sha256:880c6d21806d53e0b9b1083684c2493b9c155c762f2d956af62713c8f329318b'
  'local-agent-test/webui:v06e'     = 'sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd'
}
$services = [ordered]@{
  'knowledge-service-test' = 'knowledge-service-test'
  'planner-service-test' = 'planner-service'
  'open-webui-test' = 'open-webui-test'
}
function Read-ContainerJson {
  param([string]$Reference, [string]$Template)
  $raw = & docker container inspect --format $Template $Reference 2>$null
  if ($LASTEXITCODE -ne 0) { throw "Metadata inspection failed for $Reference; stop and inspect before retrying." }
  try { ConvertFrom-Json -InputObject ($raw -join "`n") -ErrorAction Stop }
  catch { throw "Invalid selected metadata for $Reference; stop adoption." }
}
function Get-ContainerId {
  param([string]$Reference)
  $containerId = Read-ContainerJson $Reference '{{json .Id}}'
  if ($containerId -isnot [string] -or $containerId -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid container ID.' }
  return $containerId
}
function Find-ContainerId {
  param([string]$Name)
  $found = @(& docker container ls --all --quiet --no-trunc --filter "name=^/$Name$")
  if ($LASTEXITCODE -ne 0 -or $found.Count -gt 1) { throw "Cannot establish whether name $Name is occupied; stop before mutation." }
  if ($found.Count -eq 0) { return $null }
  $containerId = Get-ContainerId $found[0]
  if ((Read-ContainerJson $containerId '{{json .Name}}') -cne "/$Name") { throw "Unexpected owner of name $Name." }
  return $containerId
}
function Get-ComposeLabels {
  param([string]$ContainerId)
  $labels = Read-ContainerJson $ContainerId '{{json .Config.Labels}}'
  if ($null -eq $labels -or $labels -isnot [pscustomobject]) { throw 'Invalid Compose label metadata; refusing mutation.' }
  return $labels
}
function Get-RestartArgument {
  param($Policy)
  if ($null -eq $Policy -or $Policy.Name -cnotin @('no', 'always', 'unless-stopped', 'on-failure') -or
      ($Policy.MaximumRetryCount -isnot [int] -and $Policy.MaximumRetryCount -isnot [long]) -or
      $Policy.MaximumRetryCount -lt 0) { throw 'Invalid restart policy; capture/review exact metadata before proceeding.' }
  if ($Policy.Name -cne 'on-failure' -and $Policy.MaximumRetryCount -ne 0) {
    throw 'Nonzero retry count on a non-on-failure policy cannot be restored exactly; stop adoption.'
  }
  if ($Policy.Name -ceq 'on-failure' -and $Policy.MaximumRetryCount -gt 0) {
    return ('on-failure:' + $Policy.MaximumRetryCount.ToString([Globalization.CultureInfo]::InvariantCulture))
  }
  return $Policy.Name  # on-failure with zero means unlimited retries, not a missing value.
}
function Assert-Original {
  param([string]$Name, [string]$CurrentName)
  $containerId = Get-ContainerId $CurrentName
  if ($containerId -cne $originalIds[$Name] -or
      (Read-ContainerJson $containerId '{{json .Name}}') -cne "/$CurrentName" -or
      (Get-ComposeLabels $containerId).'com.docker.compose.project' -cne $originalProjects[$Name]) {
    throw "Original identity/project mismatch for $Name at $CurrentName; no mutation permitted."
  }
  return $containerId
}
function Assert-RestartPolicy {
  param([string]$ContainerId, $Expected)
  $actual = Read-ContainerJson $ContainerId '{{json .HostConfig.RestartPolicy}}'
  $null = Get-RestartArgument $actual
  if ($actual.Name -cne $Expected.Name -or $actual.MaximumRetryCount -ne $Expected.MaximumRetryCount) {
    throw "Restart policy verification failed for $ContainerId; inspect Name and MaximumRetryCount."
  }
}
function Set-OriginalRestartPolicy {
  param([string]$Name, [string]$CurrentName, $Policy)
  $restartArgument = Get-RestartArgument $Policy
  $containerId = Assert-Original $Name $CurrentName
  & docker container update "--restart=$restartArgument" $containerId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Restart update failed for verified original $Name; stop and use the recovery record." }
  $null = Assert-Original $Name $CurrentName
  Assert-RestartPolicy $containerId $Policy
}
function Assert-Backup {
  param([string]$Name)
  $containerId = Assert-Original $Name $backups[$Name]
  $running = Read-ContainerJson $containerId '{{json .State.Running}}'
  if ($running -isnot [bool] -or $running) {
    throw "Backup $($backups[$Name]) ($containerId) is not stopped. Halt acceptance; stop only after verifying this recorded original ID."
  }
  Assert-RestartPolicy $containerId $restartDisabled
}
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$backups = [ordered]@{}
$originalIds = @{}
$originalRestartPolicies = @{}
$originalProjects = @{}
$replacementIds = @{}
$restartDisabled = [pscustomobject]@{ Name = 'no'; MaximumRetryCount = 0 }
$imageAliases = @('local-agent-test/knowledge:v06e', 'local-agent-test/planner:v06e', 'local-agent-test/webui:v06e')
$imageIndex = 0
foreach ($name in $services.Keys) {
  $containerId = Get-ContainerId $name
  if ((Read-ContainerJson $containerId '{{json .Name}}') -cne "/$name") { throw 'Original name/ID mismatch.' }
  $originalIds[$name] = $containerId
  $policy = Read-ContainerJson $containerId '{{json .HostConfig.RestartPolicy}}'
  $null = Get-RestartArgument $policy
  $originalRestartPolicies[$name] = $policy
  $originalProjects[$name] = (Get-ComposeLabels $containerId).'com.docker.compose.project'
  if ((Read-ContainerJson $containerId '{{json .Image}}') -cne $snapshots[$imageAliases[$imageIndex]]) {
    throw "Original $name image differs from the reviewed snapshot; stop adoption."
  }
  $imageIndex++
  $backup = "$name-v06e-backup-$stamp"
  if ($null -ne (Find-ContainerId $backup)) { throw 'Backup name already exists; never overwrite it.' }
  $backups[$name] = $backup
}
[ordered]@{ OriginalIds = $originalIds; RestartPolicies = $originalRestartPolicies;
  OriginalProjects = $originalProjects; Backups = $backups } | ConvertTo-Json -Depth 6
# Save the complete non-secret record before the first mutation below.
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

Proceed only after all gates, the saved recovery record, and UI exports pass.
Use the IDs/policies/unique backup names captured in preflight; never recapture
them after changing policies. Disable restart on **all three verified originals
before stopping or renaming any of them**. None of the
commands below operate on Chroma, the knowledge candidate, or production.

```powershell
foreach ($name in $backups.Keys) {
  Set-OriginalRestartPolicy $name $name $restartDisabled
}
foreach ($name in $backups.Keys) {
  $containerId = Assert-Original $name $name
  Assert-RestartPolicy $containerId $restartDisabled
  & docker container stop $containerId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Stop failed; inspect verified originals before continuing.' }
}
foreach ($name in $backups.Keys) {
  $containerId = Assert-Original $name $name
  if ($null -ne (Find-ContainerId $backups[$name])) { throw 'Backup name became occupied; never overwrite it.' }
  & docker container rename $containerId $backups[$name]
  if ($LASTEXITCODE -ne 0) { throw 'Rename failed; restore already-renamed originals before retrying.' }
  Assert-Backup $name
}
foreach ($name in $backups.Keys) { Assert-Backup $name }
Invoke-DocumentCompose -ComposeArgs @('up', '-d', '--no-build', '--pull', 'never')
```

No builds or pulls are permitted. Do not run the old Compose definitions against
the renamed backups. Preserve backups until acceptance; this runbook does not
delete them. Only the test WebUI publishes a port: `127.0.0.1:3001:8080`.
If any step fails, halt acceptance and use the guarded rollback below, including
for a partially completed stop/rename phase. Never start a backup beside the
replacements or rerun an old Compose project against its retained aliases.

## 5. Readiness and acceptance

### Mandatory alias-ownership and DNS gate

Run this immediately after cutover, before HTTP/UI acceptance. Record and verify
the replacement IDs/project/service labels. On **each** shared external network,
require exactly the intended running owner of every adopted test service alias
(including both planner names). Stopped backups may retain aliases; they must
remain stopped with restart disabled. Inspect `Aliases`, optional `DNSNames`, and
container names so a renamed or manually created competitor cannot hide behind
different Compose labels.

Then resolve all aliases from all three intended containers, including WebUI.
Every answer must be one of the intended target's **current inspected IPv4/IPv6
addresses** on their shared networks. Docker may return one of several shared
network addresses, so require a nonempty subset rather than one fixed IP or all
addresses. Ownership inspection catches collisions even when a DNS query happens
to return only the current API. No unknown container is ever stopped or changed
by this gate. A failure blocks acceptance: inspect the reported ID/name/network,
resolve ownership with its operator, and rerun the gate. Stop a known backup only
after matching its recorded original ID; never stop an unknown owner automatically.

```powershell
$sharedNetworks = @('agent-internal', 'planner-egress-test')
$testAliases = [ordered]@{
  'knowledge-service-test' = 'knowledge-service-test'
  'planner-service' = 'planner-service-test'
  'planner-service-test' = 'planner-service-test'
  'open-webui-test' = 'open-webui-test'
}
function Assert-Replacement {
  param([string]$Name)
  $containerId = Get-ContainerId $Name
  $labels = Get-ComposeLabels $containerId
  if (-not $replacementIds.ContainsKey($Name) -or $containerId -cne $replacementIds[$Name] -or
      $originalIds.Values -contains $containerId -or
      (Read-ContainerJson $containerId '{{json .Name}}') -cne "/$Name" -or
      $labels.'com.docker.compose.project' -cne 'agent-document-test' -or
      $labels.'com.docker.compose.service' -cne $services[$Name]) {
    throw "Replacement identity/project/service mismatch for $Name; halt acceptance."
  }
  $running = Read-ContainerJson $containerId '{{json .State.Running}}'
  if ($running -isnot [bool] -or -not $running) { throw "Replacement $Name ($containerId) is not running." }
  return $containerId
}
function Assert-AliasOwners {
  param([string]$Network)
  $raw = & docker network inspect --format '{{json .Containers}}' $Network
  if ($LASTEXITCODE -ne 0) { throw "Cannot inspect shared network $Network; halt acceptance." }
  $members = ConvertFrom-Json -InputObject ($raw -join "`n") -ErrorAction Stop
  if ($null -eq $members -or $members -isnot [pscustomobject]) { throw "Invalid network members for $Network." }
  $owners = @{}
  foreach ($alias in $testAliases.Keys) { $owners[$alias] = @() }
  foreach ($member in $members.PSObject.Properties) {
    $peerId = Get-ContainerId $member.Name
    $running = Read-ContainerJson $peerId '{{json .State.Running}}'
    if ($running -isnot [bool]) { throw "Invalid running state for $peerId on $Network." }
    if (-not $running) { continue }
    $peerName = Read-ContainerJson $peerId '{{json .Name}}'
    $networks = Read-ContainerJson $peerId '{{json .NetworkSettings.Networks}}'
    $attachment = $networks.PSObject.Properties[$Network]
    if ($null -eq $attachment) { throw "Network membership changed for $peerId on $Network; rerun the gate." }
    $dnsNames = @($attachment.Value.Aliases) + @($attachment.Value.DNSNames) + @($peerName.TrimStart('/'))
    foreach ($alias in $testAliases.Keys) {
      if ($dnsNames -contains $alias) {
        if ($peerId -cne $replacementIds[$testAliases[$alias]]) {
          throw "Alias collision: $alias on $Network also belongs to running $peerName ($peerId). Halt acceptance; inspect this owner and resolve with its operator. Never automatically stop an unknown container."
        }
        $owners[$alias] += $peerId
      }
    }
  }
  foreach ($alias in $testAliases.Keys) {
    if ($owners[$alias].Count -ne 1) { throw "Alias $alias on $Network lacks its intended running owner; inspect attachments and rerun the gate." }
  }
}
function Assert-DnsAnswers {
  param([string]$Caller, [string]$Alias, $Actual, [string[]]$Expected)
  if ($Actual -isnot [array] -or $Actual.Count -eq 0 -or $Expected.Count -eq 0) {
    throw "Empty/invalid DNS answer for $Alias from $Caller; inspect shared-network DNS and rerun the gate."
  }
  foreach ($address in $Actual) {
    if ($address -isnot [string]) { throw "Non-string DNS address for $Alias from $Caller; halt acceptance." }
    try { $canonical = [Net.IPAddress]::Parse($address).ToString() }
    catch { throw "Invalid DNS address for $Alias from $Caller; halt acceptance." }
    if ($Expected -notcontains $canonical) {
      throw "DNS mismatch: $Alias from $Caller returned $canonical; intended current IPs: $($Expected -join ', '). Inspect alias owners; halt acceptance and rerun after resolution. No containers were stopped by this gate."
    }
  }
}
foreach ($name in $backups.Keys) { Assert-Backup $name }
foreach ($name in $services.Keys) {
  # Capture once. Subsequent gate runs must retain these IDs, not adopt new owners.
  if (-not $replacementIds.ContainsKey($name)) { $replacementIds[$name] = Get-ContainerId $name }
  $null = Assert-Replacement $name
}
$replacementIds | ConvertTo-Json  # Save these additional non-secret IDs for rollback.
foreach ($network in $sharedNetworks) { Assert-AliasOwners $network }
$currentAddresses = @{}
foreach ($name in $services.Keys) {
  $containerId = Assert-Replacement $name
  $networks = Read-ContainerJson $containerId '{{json .NetworkSettings.Networks}}'
  $addresses = @()
  foreach ($network in $sharedNetworks) {
    $attachment = $networks.PSObject.Properties[$network]
    if ($null -eq $attachment) { throw "Intended $name is missing shared network $network." }
    foreach ($address in @($attachment.Value.IPAddress, $attachment.Value.GlobalIPv6Address)) {
      if (-not [string]::IsNullOrWhiteSpace($address)) { $addresses += [Net.IPAddress]::Parse($address).ToString() }
    }
  }
  if ($addresses.Count -eq 0) { throw "No current shared-network IPs for intended $name." }
  $currentAddresses[$name] = @($addresses | Sort-Object -Unique)
}
$dnsProbe = @'
import json, socket, sys
answers = {alias: sorted({item[4][0] for item in socket.getaddrinfo(alias, None, socket.AF_UNSPEC, socket.SOCK_STREAM)}) for alias in sys.argv[1:]}
print(json.dumps(answers))
'@
$aliasNames = @($testAliases.Keys)
foreach ($name in $services.Keys) {
  $containerId = Assert-Replacement $name
  $raw = $dnsProbe | & docker exec -i $containerId python - @aliasNames
  if ($LASTEXITCODE -ne 0) { throw "DNS probe failed from $name; inspect shared-network DNS before acceptance." }
  $answers = ConvertFrom-Json -InputObject ($raw -join "`n") -ErrorAction Stop
  foreach ($alias in $aliasNames) {
    Assert-DnsAnswers $name $alias $answers.$alias $currentAddresses[$testAliases[$alias]]
  }
}
foreach ($network in $sharedNetworks) { Assert-AliasOwners $network }
foreach ($name in $backups.Keys) { Assert-Backup $name }
foreach ($name in $services.Keys) { $null = Assert-Replacement $name }
Write-Output 'Test service alias ownership and current-IP DNS checks passed.'
```

This is a cutover gate, not continuous monitoring. If attachments, IDs, or runtime
state change during acceptance, halt and rerun it before using the UI again.

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

### Operator-reported v0.6g checkpoint

Reported by the operator, not re-run for this documentation change: **95 offline
tests passed**; real PDF extraction passed through WebUI; unrelated-document
extraction, filename ambiguity, and partial/missing extraction also passed.
Chat presentation still has limitations: check returned fields, statuses, and
source quotes against the structured tool result. These exercised cases do not
establish universal document/PDF support or semantic correctness.

## 6. Rollback (only the three new containers)

If adoption partially fails, do not rerun renames blindly. Use the recorded
`$backups`, `$originalIds`, `$originalProjects`, and `$originalRestartPolicies`
to identify what moved. Keep the preflight helpers in the same PowerShell session;
if the session was lost, load these maps from the saved non-secret recovery
record and define the helpers first, without re-running capture/tagging/adoption.
Also restore the `$services` constant; use the saved `$replacementIds` if available,
or initialize it to `@{}` for a cutover that failed before those IDs were recorded.
The rollback also handles originals still at their original names. It verifies
all removal candidates first and removes only replacements with the exact TEST
project/service/name/ID guards, never volumes or networks. **Only after replacement
removal** does it restore names, exact captured restart policies (including
`on-failure` retry counts), verify them, and start verified originals. No default
restart policy is substituted. Unknown occupants cause an actionable refusal.

```powershell
$removals = [ordered]@{}
function Assert-RemovalCandidate {
  param([string]$Name, [string]$ContainerId)
  $labels = Get-ComposeLabels $ContainerId
  if ($originalIds.Values -contains $ContainerId -or (Get-ContainerId $Name) -cne $ContainerId -or
      (Read-ContainerJson $ContainerId '{{json .Name}}') -cne "/$Name" -or
      $labels.'com.docker.compose.project' -cne 'agent-document-test' -or
      $labels.'com.docker.compose.service' -cne $services[$Name] -or
      ($replacementIds.ContainsKey($Name) -and $replacementIds[$Name] -cne $ContainerId)) {
    throw "Refusing removal of $Name ($ContainerId): ID/project/service mismatch. Inspect this occupant; never remove an unknown container."
  }
}
foreach ($name in $backups.Keys) {
  $containerId = Find-ContainerId $name
  if ($null -eq $containerId) { continue }
  if ($containerId -ceq $originalIds[$name]) {
    $null = Assert-Original $name $name  # Not renamed yet; never remove it.
    continue
  }
  Assert-RemovalCandidate $name $containerId
  $removals[$name] = $containerId
}
foreach ($name in $removals.Keys) {
  $containerId = $removals[$name]
  Assert-RemovalCandidate $name $containerId
  & docker container rm --force $containerId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'New TEST container removal failed; do not restore original restart policies yet.' }
}
foreach ($name in $backups.Keys) {
  $occupant = Find-ContainerId $name
  if ($null -ne $occupant) {
    $null = Assert-Original $name $name
    continue
  }
  $containerId = Assert-Original $name $backups[$name]
  & docker container rename $containerId $name
  if ($LASTEXITCODE -ne 0) { throw 'Original name restoration failed; keep backups restart-disabled until resolved.' }
  $null = Assert-Original $name $name
}
foreach ($name in $backups.Keys) {
  Set-OriginalRestartPolicy $name $name $originalRestartPolicies[$name]
}
foreach ($name in $backups.Keys) {
  $containerId = Assert-Original $name $name
  Assert-RestartPolicy $containerId $originalRestartPolicies[$name]
  & docker container start $containerId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Original TEST container startup failed; inspect the verified original ID.' }
}
```

The shared external WebUI volume and external Chroma retain their state. Renamed
containers are not data backups; rollback restores container configuration, not
UI/database history. Test watcher indexing may change the isolated index during
adoption. Recheck original readiness and saved UI configuration after rollback.
An unknown competing alias owner remains an operator issue; rollback never
removes it. Verify alias ownership and current-IP DNS again for the restored
original IDs before resuming UI use.
Finally remove only the empty scratch env file with
`Remove-Item -LiteralPath $emptyEnvFile.FullName`; never remove volumes/networks
or prune resources. All Compose invocations restore the caller's token environment.
