# Production document-analysis adoption (preparation only)

This tooling has not deployed or cleaned up production. Application runtime is
the reviewed `scripts.document_service_production` and standalone
`tools/analyze_document_production.py`; pipeline version remains **0.6i**.
Use Windows PowerShell **5.1** and the observed Compose **5.6.1**. Review and
commit the runtime first. Supply its full expected HEAD explicitly; the helper
requires tracked working-tree/index cleanliness and exports **the entire tracked
commit** with `git archive --output`. Untracked files, including local test
fixtures, do not enter the release. No builds, pulls, deployment-time installs,
or working-tree application source binds are permitted.

The managed project is `agent-production`, with exactly `open-webui`,
`knowledge-service`, and `document-planner-prod`. Chroma remains the **same
external container**, with the existing `knowledge-db` bind at `/data`.
`agent-internal` remains external; the helper may create the labelled
`agent-production-egress` network. No test network or `planner-service` alias is
used. Knowledge and API ports are never published. Port **3000** retains the
canonical WebUI's captured host-IP/protocol/target bindings through the private
override; there is no potentially broader default binding in the Compose file.

## Preconditions and private records

Before any operator action, freeze production UI writes, uploads, indexing input
changes, concurrent Docker/Compose/Git operations, and other database clients.
Export/verify current UI models and tools locally without printing secret valves.
Record representative chats and documents for acceptance. Keep existing model,
tool, and preset configurations; the procedure never imports/replaces them.
Verify free disk capacity for the complete cold pair, a complete failed-state
pair if rollback is needed, exported releases, and inventories. Status reports
recorded checkpoints, not a fresh live acceptance or backup-integrity guarantee;
cutover/restoration revalidate the archives before use.

Prepare verifies these names and captures their **full IDs, exact restart policy
and MaximumRetryCount, image IDs, mounts, network IDs and configuration hashes**:

- Canonical `open-webui`, `knowledge-service`, and external `chromadb`.
- Historical `open-webui-before-scripts`, `open-webui-backup`, and
  `knowledge-service-v1-backup`.

Null Compose project labels on originals are expected. Unknown project/name,
port-3000 owners, shared-volume writers, or production Chroma clients cause a
refusal; the helper never automatically stops an unknown container. Guarded local
image aliases must match the four cached image IDs embedded in the helper.
Missing images must be restored from reviewed offline image archives, not pulled.

`runtime/production/private/<run-id>` holds private raw UTF-8 env files, separate
production API token, knowledge credential copy, immutable original recovery
record, atomic phase state/previous state, overrides, and backup archives. It must
be on a Windows filesystem supporting the verified owner/SYSTEM-only ACLs.
Protection failure aborts. Do not print these files, use `Get-Content` on env/token
files, run environment dumps, render real Compose environments, or attach them to
logs/issues. The original `WEBUI_SECRET_KEY` remains an **environment value**,
preserved exactly; no signing-key file is invented. Raw `env_file` format retains
quotes, dollars, hashes, equals, spaces, and empty values without interpolation.
Multiline/duplicate/malformed env entries are refused.
Compose's rendered configuration escapes literal dollars as `$$`; the helper
does not render real environments or rewrite dollars in the raw env files.

The existing `knowledge-service/knowledge-token.txt` must match `KNOWLEDGE_TOKEN`
exactly after the credential file's normal trim. The API gets a separate
cryptographically random token mounted only where needed. API and knowledge
credential paths are fixed by the reviewed application.

Prepare refuses external/custom WebUI SQL or vector stores and uncovered writable
mounts: its backup plan covers local `webui.db` and `/data/chroma.sqlite3` only.
The observed production knowledge host/port/model/watcher settings must match;
document binds must still be root `input/` and `knowledge/`. Cached helper `sh`,
`tar`, `find`, `sha256sum`, and `sort` must work offline. The conservative archive
policy supports regular files/directories, including dotfiles; **links, hardlinks,
special entries, duplicate paths, and traversal names are refused**. If cache or
data contains links, obtain a separately reviewed backup implementation; do not
delete or flatten cache/data to bypass this gate. Custom startup/device settings
also require review. Existing read-only tracked source mounts are redirected to
the identical path in the exported release; other supported mounts are retained.

## Explicit actions

Dot-sourcing defines functions only, even when an Action parameter is supplied.
No action runs just by importing the script. Run from the reviewed workspace:

```powershell
# Operator only; replace with the reviewed full commit. No commands here were deployed.
& .\scripts\deploy_document_production.ps1 -Action Prepare -ExpectedCommit '<40-character-reviewed-commit>'
# Keep the exact private StatePath printed by Prepare; do not select a different run.
$state = 'E:\AI-Agent-Workspace\runtime\production\private\<run-id>\state.json'
& .\scripts\deploy_document_production.ps1 -Action Status -StatePath $state
& .\scripts\deploy_document_production.ps1 -Action Cutover -StatePath $state
```

Prepare does not stop originals or start production services. It exports private
configuration, guards/tags cached images, exports a unique commit-pinned release
under `runtime/production/releases/<commit>/<run-id>`, and validates imports in
cached containers with **no network or credential mounts**. It checks SQLite and
catalog baselines and uses **synthetic env files only** for `compose config
--quiet`. The actual deployment uses its frozen template/private override, an
explicit empty `--env-file`, and restored process interpolation variables. Do not
run the base Compose file alone or the old definitions against renamed backups.

Cutover rechecks IDs/configuration, disables restart and stops all historical
duplicates, then stops canonical UI/knowledge and the same Chroma. Only when all
writers are stopped does the cached helper create complete cold tar archives
and per-file SHA-256 inventories. It has no network, a read-only source, a
read-only root filesystem, and only the required data/output mounts. Archive
contents/types, database presence, counts and hashes must verify before renaming
canonical UI/knowledge to unique backups. Those backups remain stopped with
restart disabled. The same Chroma ID restarts with its captured restart policy;
then the three services start using `--no-build --pull never --no-recreate`.

Automated gates check exact mounts, image IDs, raw environment preservation,
restart state, production networks, competing running alias owners and DNS
answers against **current inspected IPs**, plus readiness and non-reset data.
Owners are inspected even if a DNS query happens to return only the correct IP.
The gates verify WebUI user/chat counts, the authenticated production catalog
identity set and index counts, API production profile and pipeline 0.6i, correct
auth rejection/acceptance, and LM Studio model-list reachability. The auth probe
uses an invalid request body and never invokes a model. Failures retain state and
data for explicit recovery; there is no automatic rollback or cleanup. A partial
cutover must use recorded Rollback, not a blind rerun of Cutover.

## Main-UI production acceptance

`ReadyForOperatorAcceptance` is **not** final acceptance. Keep the maintenance
freeze until these operator checks pass:

1. Open the main UI at its unchanged port-3000 address. Log in with the existing
   account, confirm prior sessions/chats, saved model presets, tools and settings,
   and compare the locally saved UI exports. Do not overwrite existing presets.
2. Create a **distinct** production document-analysis tool from the mounted
   `/workspace/tools/analyze_document_production.py`. Saving the tool in WebUI is
   required; the read-only mount does not install/update saved tools. Its only
   method is `analyze_document`, its API is
   `http://document-planner-prod:8105/analyze-document`, and its credential is the
   fixed `/run/secrets/document_planner_prod_token`. Do not reuse test valves.
3. Create a separate document-analysis preset using the already approved model
   and native function calling. Enable only this production document tool for
   the smoke checks; preserve all existing production model/tool configurations.
4. Check `/health` reports production and pipeline **0.6i**. Verify an
   unauthenticated analysis/catalog request returns 401, authenticated catalog
   access returns the expected indexed production documents, and no API/knowledge
   host ports were published. `/analyze`, COMMIT, execution, approval, indexing,
   and file-write routes must not be available on the document API.
5. Through the new preset/tool, test explicit extraction from unrelated indexed
   documents, exact quotations/locations/units, a known missing/partial field, and
   filename/value ambiguity. Compare structured tool results to original sources;
   chat presentation and mocked tests do not establish semantic accuracy or
   universal document support. Verify unsupported mixed requests remain rejected.

Record acceptance and the StatePath/commit privately. Only after successful
production acceptance should a separate reviewed cleanup plan be considered.
This helper has no cleanup, pruning, volume/network removal, test-stack removal,
or old-source deletion action.

## Rollback and partial failures

```powershell
# Preserve current data unless the operator explicitly selects cold data restoration.
& .\scripts\deploy_document_production.ps1 -Action Rollback -StatePath $state
# Destructive to current data only inside the two guarded targets, after failed-state backups:
& .\scripts\deploy_document_production.ps1 -Action Rollback -StatePath $state -RestoreData
```

Rollback uses recorded IDs, fingerprints, ownership/run/commit labels, phase,
and exact restart policies. It handles originals not yet renamed and partially
created replacements. Only verified replacements are stopped/removed, without
volume removal. Historical duplicates remain stopped and restart-disabled.
Unknown occupants abort instead of being overwritten. With `-RestoreData`, all
writers including Chroma first stop; verified failed-state archives of **both**
targets are retained before any restore. Cold archive hashes, inventory hashes,
entry paths/types and exact volume/bind identities are checked again. Extraction
only targets the existing `open-webui` volume and exact `knowledge-db` bind;
post-restore file hashes must match. Originals then regain their names and exact
restart policies; Chroma starts before knowledge/UI. Without `-RestoreData`, the
current data is retained, so review application/schema compatibility explicitly.

If the state file is damaged, stop: the ACL-protected `.previous` and immutable
`original-recovery.json` are recovery evidence, not permission to guess missing
phases or overwrite state. Retain all failed-state/cold archives and releases.
Recheck original readiness, signing-key/login/chat continuity, alias ownership
and production data after rollback. No broad Docker or filesystem cleanup belongs
in incident recovery.

## Offline tooling validation

```powershell
& .\scripts\test_deploy_document_production.ps1
# Explicitly permits only synthetic, offline Compose config parsing (no daemon actions):
& .\scripts\test_deploy_document_production.ps1 -ValidateCompose
```

The tests use synthetic metadata and mocked Docker/Git commands. They do not read
real credentials, operate production/test containers, run application tests, or
prove that the live prerequisites or acceptance cases pass. Synthetic scratch
files are retained in the Windows temporary directory for inspection.
