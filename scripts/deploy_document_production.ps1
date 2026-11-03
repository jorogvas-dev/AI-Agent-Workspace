# Windows PowerShell 5.1. Importing/dot-sourcing defines functions only.
[CmdletBinding()]
param(
  [ValidateSet('Prepare','Cutover','Status','Rollback')][string]$Action,
  [string]$ExpectedCommit,
  [string]$StatePath,
  [switch]$RestoreData
)

function Stop-Prod([string]$Message) { throw [InvalidOperationException]::new('PROD-GUARD: ' + $Message) }
function Get-ProdRoot { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) }
function Get-ProdImages {
  [ordered]@{
    knowledge = 'sha256:7dbc132569c39f39cbaaa64720e7a3d916de3a890d99a9d02680d47e01c4b5b1'
    webui = 'sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd'
    planner = 'sha256:880c6d21806d53e0b9b1083684c2493b9c155c762f2d956af62713c8f329318b'
    helper = 'sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159'
  }
}
function Get-Field($Object, [string]$Name, $Default = $null) {
  if ($null -eq $Object) { return $Default }
  if ($Object -is [Collections.IDictionary]) { if ($Object.Contains($Name)) { return $Object[$Name] }; return $Default }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -ne $property) { return $property.Value }; return $Default
}
function Get-TextHash([string]$Text) {
  $sha = [Security.Cryptography.SHA256]::Create()
  try { ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
  finally { $sha.Dispose() }
}
function Get-ObjectHash($Object) { Get-TextHash (ConvertTo-Json -InputObject $Object -Depth 60 -Compress) }
function Assert-Id([string]$Id) { if ($Id -cnotmatch '^[a-f0-9]{64}$') { Stop-Prod 'Invalid full container identity.' } }
function Assert-ChildPath([string]$Path, [string]$Parent) {
  $resolved = [IO.Path]::GetFullPath($Path)
  $prefix = [IO.Path]::GetFullPath($Parent).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
  if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or $resolved.Contains(',')) {
    Stop-Prod 'Path escapes its approved root or cannot be represented safely as a Docker mount.'
  }
  $resolved
}
function Assert-NoReparse([string]$Path, [switch]$Tree) {
  $cursor = [IO.Path]::GetFullPath($Path)
  while ($cursor) {
    if (Test-Path -LiteralPath $cursor) {
      if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { Stop-Prod 'Reparse points are not supported.' }
    }
    $cursor = [IO.Path]::GetDirectoryName($cursor)
  }
  if ($Tree -and (Test-Path -LiteralPath $Path -PathType Container)) {
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($Path)) { Assert-NoReparse $entry -Tree }
  }
}
function Protect-ProdPath([string]$Path) {
  Assert-NoReparse $Path
  $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
  $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
  $directory = Test-Path -LiteralPath $Path -PathType Container
  $acl = if ($directory) { [Security.AccessControl.DirectorySecurity]::new() } else { [Security.AccessControl.FileSecurity]::new() }
  $acl.SetOwner($user); $acl.SetAccessRuleProtection($true, $false)
  $inherit = if ($directory) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
  foreach ($sid in @($user, $system)) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', $inherit, 'None', 'Allow'))
  }
  Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
  $check = Get-Acl -LiteralPath $Path -ErrorAction Stop
  if (-not $check.AreAccessRulesProtected -or $check.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $user.Value) {
    Stop-Prod 'Private ACL ownership/protection verification failed.'
  }
  $rules = @($check.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne 2 -or @($rules | Where-Object {
    $_.IsInherited -or $_.AccessControlType -ne 'Allow' -or $_.IdentityReference.Value -notin @($user.Value, $system.Value) -or
    $_.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl
  }).Count) { Stop-Prod 'Private ACL verification failed; do not write private data.' }
}
function New-PrivateDirectory([string]$Path) {
  Assert-NoReparse $Path
  $null = New-Item -ItemType Directory -Path $Path -Force -ErrorAction Stop
  Protect-ProdPath $Path
}
function Write-PrivateText([string]$Path, [string]$Text) {
  Protect-ProdPath ([IO.Path]::GetDirectoryName($Path))
  Assert-NoReparse $Path
  [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false, $true))
  Protect-ProdPath $Path
}
function Convert-EnvEntries($Entries) {
  $map = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
  foreach ($entry in $Entries) {
    if ($entry -isnot [string] -or $entry.IndexOfAny([char[]]"`r`n`0") -ge 0 -or $entry.IndexOf('=') -le 0) { Stop-Prod 'Unsupported multiline or malformed container environment.' }
    $index = $entry.IndexOf('='); $key = $entry.Substring(0,$index)
    if ($key -cnotmatch '^[A-Za-z_][A-Za-z0-9_]*$' -or $map.ContainsKey($key)) { Stop-Prod 'Invalid or duplicate environment key.' }
    $map.Add($key, $entry.Substring($index + 1))
  }
  return ,$map
}
function Write-RawEnv([string]$Path, $Entries) {
  $null = Convert-EnvEntries $Entries
  Write-PrivateText $Path (($Entries -join "`n") + "`n")
}
function Invoke-ProdNative {
  param([ValidateSet('Docker','Git')][string]$Kind, [string[]]$Arguments, [string]$Operation, [string]$InputText)
  $command = if ($Kind -eq 'Docker') { 'docker' } else { 'git' }
  $previous = [Console]::OutputEncoding; $previousPipe = $OutputEncoding
  try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding
    # Capture everything privately. Never echo native stdout/stderr on failure.
    $output = if ($PSBoundParameters.ContainsKey('InputText')) { @($InputText | & $command @Arguments 2>&1) } else { @(& $command @Arguments 2>&1) }
    if ($LASTEXITCODE -ne 0) { Stop-Prod "$Kind failed during $Operation; inspect the private state and prerequisites." }
    $output -join "`n"
  } catch {
    if ($_.Exception.Message.StartsWith('PROD-GUARD:')) { throw }
    Stop-Prod "$Kind failed during $Operation; native details were withheld."
  } finally { [Console]::OutputEncoding = $previous; $OutputEncoding = $previousPipe }
}
function Read-DockerJson([string[]]$Arguments) {
  $raw = Invoke-ProdNative Docker $Arguments 'metadata inspection'
  try { ConvertFrom-Json -InputObject $raw -ErrorAction Stop } catch { Stop-Prod 'Invalid Docker metadata; stop before mutation.' }
}
function Find-ProdContainer([string]$Name) {
  $raw = Invoke-ProdNative Docker @('container','ls','-aq','--no-trunc','--filter',"name=^/$Name$") 'name ownership check'
  $ids = @($raw -split '\r?\n' | Where-Object { $_ })
  if ($ids.Count -gt 1) { Stop-Prod 'Container name ownership is not unique.' }
  if ($ids.Count) { Assert-Id $ids[0]; return $ids[0] }; return $null
}
function Get-RestartArgument($Policy) {
  if ($null -eq $Policy -or $Policy.Name -cnotin @('no','always','unless-stopped','on-failure') -or
      ($Policy.MaximumRetryCount -isnot [int] -and $Policy.MaximumRetryCount -isnot [long]) -or $Policy.MaximumRetryCount -lt 0 -or
      ($Policy.Name -cne 'on-failure' -and $Policy.MaximumRetryCount -ne 0)) { Stop-Prod 'Unsupported exact restart policy.' }
  if ($Policy.Name -ceq 'on-failure' -and $Policy.MaximumRetryCount -gt 0) { return "on-failure:$($Policy.MaximumRetryCount)" }
  $Policy.Name
}
function Read-ProdContainer([string]$Reference) {
  $raw = Read-DockerJson @('container','inspect','--format','{{json .}}',$Reference)
  Assert-Id $raw.Id
  if ($raw.Name -cnotmatch '^/[A-Za-z0-9][A-Za-z0-9_.-]*$') { Stop-Prod 'Invalid Docker container name.' }
  $hostConfig = ConvertFrom-Json (ConvertTo-Json $raw.HostConfig -Depth 50 -Compress)
  $hostConfig.PSObject.Properties.Remove('RestartPolicy')
  $networks = @($raw.NetworkSettings.Networks.PSObject.Properties | Sort-Object Name | ForEach-Object {
    [ordered]@{ Name=$_.Name; Id=$_.Value.NetworkID }
  })
  $fingerprint = Get-ObjectHash ([ordered]@{ Image=$raw.Image; Config=$raw.Config; Host=$hostConfig; Mounts=$raw.Mounts; Networks=$networks })
  [pscustomobject]@{
    Raw=$raw; Environment=@($raw.Config.Env)
    Record=[pscustomobject]@{ Id=$raw.Id; Name=$raw.Name.TrimStart('/'); Image=$raw.Image; Fingerprint=$fingerprint;
      RestartPolicy=$raw.HostConfig.RestartPolicy; Mounts=@($raw.Mounts); Networks=$networks; BackupName=$null }
  }
}
function Assert-Original($State, [string]$Name) {
  $record = $State.Originals.$Name
  Assert-Id $record.Id
  $current = Read-ProdContainer $record.Id
  $allowedNames=@($Name);if($Name -cin @('open-webui','knowledge-service')){$allowedNames+=$record.BackupName}
  if ($current.Record.Id -cne $record.Id -or $current.Record.Name -cnotin $allowedNames -or
      $current.Record.Fingerprint -cne $record.Fingerprint) { Stop-Prod "Original identity/configuration changed for $Name; no mutation allowed." }
  $policy=Get-RestartArgument $current.Record.RestartPolicy;$captured=Get-RestartArgument $record.RestartPolicy
  if(($State.Phase -cin @('Preparing','Prepared') -and $policy -cne $captured) -or $policy -cnotin @('no',$captured)){Stop-Prod 'Original restart policy changed outside this procedure.'}
  $current
}
function Save-ProdState($State) {
  $path=Assert-ChildPath $State.StatePath $State.Directory
  foreach($file in @($path,$path+'.new',$path+'.previous')){Assert-NoReparse $file}
  $recovery=Join-Path $State.Directory 'original-recovery.json'
  if(-not (Test-Path -LiteralPath $recovery)){Write-PrivateText $recovery (ConvertTo-Json @{Root=$State.Root;RunId=$State.RunId;Commit=$State.Commit;Originals=$State.Originals} -Depth 40)}
  Write-PrivateText ($path+'.new') (ConvertTo-Json $State -Depth 60)
  if([IO.File]::Exists($path)){[IO.File]::Replace($path+'.new',$path,$path+'.previous')}else{[IO.File]::Move($path+'.new',$path)}
  Protect-ProdPath $path
}
function Set-ProdPhase($State, [string]$Phase) { $State.Phase=$Phase; Save-ProdState $State }
function Set-OriginalPolicy($State, [string]$Name, $Policy) {
  $argument = Get-RestartArgument $Policy
  $current = Assert-Original $State $Name
  $null = Invoke-ProdNative Docker @('container','update',"--restart=$argument",$current.Record.Id) 'verified original restart policy update'
  $check = Assert-Original $State $Name
  if ((Get-ObjectHash $check.Record.RestartPolicy) -cne (Get-ObjectHash $Policy)) { Stop-Prod 'Restart policy readback mismatch.' }
}
function Stop-Original($State, [string]$Name) {
  Set-OriginalPolicy $State $Name ([pscustomobject]@{Name='no';MaximumRetryCount=0})
  $current = Assert-Original $State $Name
  if ($current.Raw.State.Running) { $null = Invoke-ProdNative Docker @('container','stop',$current.Record.Id) 'verified original stop' }
  $current = Assert-Original $State $Name
  if ($current.Raw.State.Running) { Stop-Prod 'Verified original did not stop.' }
  Save-ProdState $State
}
function Start-Original($State, [string]$Name) {
  Set-OriginalPolicy $State $Name $State.Originals.$Name.RestartPolicy
  $current = Assert-Original $State $Name
  $null = Invoke-ProdNative Docker @('container','start',$current.Record.Id) 'verified original start'
  if (-not (Assert-Original $State $Name).Raw.State.Running) { Stop-Prod 'Verified original did not start.' }
}
function Assert-DatabaseConfiguration($WebEnv, $KnowledgeEnv, $ChromaEnv) {
  foreach ($key in @('DATABASE_URL','DATABASE_TYPE','DATABASE_PROVIDER','CHROMA_HTTP_HOST','REDIS_URL','REDIS_SENTINEL_HOSTS')) {
    $value = if ($WebEnv.ContainsKey($key)) { $WebEnv[$key] } else { '' }
    $allowed = switch ($key) { 'DATABASE_URL' { @('','sqlite:////app/backend/data/webui.db') }; 'DATABASE_TYPE' { @('','sqlite') }; 'DATABASE_PROVIDER' { @('','sqlite') }; default { @('') } }
    if ($value -cnotin $allowed) { Stop-Prod 'Unsupported external/custom WebUI database; this cold-backup plan cannot preserve it.' }
  }
  if ($WebEnv.ContainsKey('VECTOR_DB') -and $WebEnv['VECTOR_DB'] -cnotin @('','chroma')) { Stop-Prod 'Unsupported external WebUI vector database.' }
  $expected = @{CHROMA_HOST='chromadb';CHROMA_PORT='8000';LM_STUDIO_URL='http://host.docker.internal:1234/v1/embeddings';
    EMBEDDING_MODEL='text-embedding-nomic-embed-text-v1.5@q8_0';WATCHER_ENABLED='true';WATCH_INTERVAL_SECONDS='60'}
  foreach ($key in $expected.Keys) { if (-not $KnowledgeEnv.ContainsKey($key) -or $KnowledgeEnv[$key] -cne $expected[$key]) { Stop-Prod 'Production knowledge configuration differs from the reviewed baseline.' } }
  if (-not $WebEnv.ContainsKey('WEBUI_SECRET_KEY') -or -not $WebEnv['WEBUI_SECRET_KEY']) { Stop-Prod 'Existing environment-held WebUI signing key is required.' }
  if (($ChromaEnv.ContainsKey('PERSIST_DIRECTORY') -and $ChromaEnv['PERSIST_DIRECTORY'] -cne '/data') -or
      ($ChromaEnv.ContainsKey('IS_PERSISTENT') -and $ChromaEnv['IS_PERSISTENT'].ToLowerInvariant() -ne 'true') -or
      ($ChromaEnv.ContainsKey('CHROMA_DB_IMPL') -and $ChromaEnv['CHROMA_DB_IMPL'] -notmatch 'sqlite')) { Stop-Prod 'Unsupported Chroma persistence configuration.' }
}

function Read-StartupSettingJson([string]$Kind, [string]$Id, [string]$ContainerName, [string]$Setting) {
  try {
    $raw=Invoke-ProdNative Docker @($Kind,'inspect','--format',("{{json .Config.$Setting}}"),$Id) 'selected startup setting inspection'
    if ($raw -isnot [string] -or -not $raw.Trim()) { throw 'Missing JSON.' }
    # Validate the complete JSON value first. A property wrapper then preserves
    # empty/single-element arrays without returning them through a PS pipeline.
    $null=ConvertFrom-Json -InputObject $raw -ErrorAction Stop
    $wrapped=ConvertFrom-Json -InputObject ('{"setting":'+$raw+'}') -ErrorAction Stop
    $value=$wrapped.setting
    if ($null -eq $value) { return 'null' }
    if ($Setting -cin @('Cmd','Entrypoint')) {
      if ($value -isnot [array]) { throw 'Expected an argument array.' }
      foreach ($argument in $value) { if ($argument -isnot [string]) { throw 'Expected a string argument.' } }
    } elseif ($value -isnot [string]) { throw 'Expected a string setting.' }
    ConvertTo-Json -InputObject $value -Depth 4 -Compress
  } catch { Stop-Prod "Startup inspection failed for $ContainerName setting $Setting; no mutation allowed; values withheld." }
}
function Assert-MainMounts($Ui, $Knowledge, $Chroma, [string]$Root) {
  foreach ($entry in @($Ui,$Knowledge,$Chroma)) {
    $targets=@($entry.Record.Mounts | ForEach-Object { $_.Destination })
    if (@($targets | Select-Object -Unique).Count -ne $targets.Count) { Stop-Prod 'Duplicate mount targets are not approved.' }
  }
  foreach ($entry in @($Ui,$Knowledge)) {
    if ($entry.Raw.HostConfig.ExtraHosts -or $entry.Raw.HostConfig.Privileged -or $entry.Raw.HostConfig.Devices -or
        $entry.Raw.HostConfig.DeviceRequests) { Stop-Prod 'Unsupported host/device overrides; review instead of silently changing settings.' }
    $names = @($entry.Raw.NetworkSettings.Networks.PSObject.Properties.Name)
    if ($names.Count -ne 2 -or $names -notcontains 'agent-internal' -or $names -notcontains 'bridge') { Stop-Prod 'Canonical network attachments differ from the observed baseline.' }
    Assert-Id $entry.Record.Id
    if ($entry.Record.Image -cnotmatch '^sha256:[a-f0-9]{64}$') { Stop-Prod 'Startup inspection requires a pinned full image ID.' }
    foreach ($key in @('Cmd','Entrypoint','WorkingDir','User')) {
      $containerJson=Read-StartupSettingJson 'container' $entry.Record.Id $entry.Record.Name $key
      $imageJson=Read-StartupSettingJson 'image' $entry.Record.Image $entry.Record.Name $key
      if (-not [string]::Equals($containerJson,$imageJson,[StringComparison]::Ordinal)) {
        Stop-Prod "Startup mismatch for $($entry.Record.Name) setting $key; review before adopting the pinned image; values withheld."
      }
    }
  }
  $uiData = @($Ui.Record.Mounts | Where-Object { $_.Destination -ceq '/app/backend/data' })
  if ($uiData.Count -ne 1 -or $uiData[0].Type -ne 'volume' -or $uiData[0].Name -cne 'open-webui' -or -not $uiData[0].RW) { Stop-Prod 'Main WebUI volume identity differs from open-webui.' }
  foreach ($name in @('output','scripts','temp')) {
    $mount=@($Ui.Record.Mounts | Where-Object { $_.Destination -ceq "/workspace/$name" })
    if ($mount.Count -ne 1 -or $mount[0].Type -cne 'bind' -or -not $mount[0].RW -or
        [IO.Path]::GetFullPath($mount[0].Source) -ine (Join-Path $Root $name)) { Stop-Prod 'Observed WebUI workspace bind source/type/mode differs from the approved layout.' }
  }
  foreach ($mount in $Ui.Record.Mounts) {
    if ($mount.Destination -eq '/app/backend/.webui_secret_key' -or
        ($mount.RW -and $mount.Destination -cnotin @('/app/backend/data','/workspace/output','/workspace/scripts','/workspace/temp'))) { Stop-Prod 'Uncovered WebUI writable storage/signing-key file requires a separate reviewed backup plan.' }
  }
  foreach ($pair in @(@('input','/workspace/input'),@('knowledge','/workspace/knowledge'))) {
    $mount = @($Knowledge.Record.Mounts | Where-Object { $_.Destination -ceq $pair[1] })
    if ($mount.Count -ne 1 -or $mount[0].Type -ne 'bind' -or [IO.Path]::GetFullPath($mount[0].Source) -ine (Join-Path $Root $pair[0])) { Stop-Prod 'Knowledge document bind identity mismatch.' }
  }
  if (@($Knowledge.Record.Mounts | Where-Object { $_.Destination -notin @('/workspace/input','/workspace/knowledge','/app/server.py') }).Count -or
      ($Knowledge.Raw.HostConfig.PortBindings -and @($Knowledge.Raw.HostConfig.PortBindings.PSObject.Properties).Count)) { Stop-Prod 'Unsupported knowledge mounts or published ports.' }
  $db = @($Chroma.Record.Mounts | Where-Object { $_.Destination -ceq '/data' })
  if ($db.Count -ne 1 -or $db[0].Type -ne 'bind' -or -not $db[0].RW -or
      [IO.Path]::GetFullPath($db[0].Source) -ine (Join-Path $Root 'knowledge-db') -or
      @($Chroma.Record.Mounts | Where-Object { $_.RW -and $_.Destination -ne '/data' }).Count) { Stop-Prod 'Chroma must use only the approved knowledge-db bind for persistent storage.' }
  Assert-NoReparse (Join-Path $Root 'knowledge-db') -Tree
}
function Convert-ProdMount($Mount, $State, [string[]]$Tracked) {
  $result = [ordered]@{ type=$Mount.Type; target=$Mount.Destination; read_only=(-not $Mount.RW) }
  if ($Mount.Type -eq 'volume') {
    if (-not $Mount.Name) { Stop-Prod 'Anonymous WebUI volumes are not supported.' }
    $result.source=$Mount.Name
  } elseif ($Mount.Type -eq 'bind') {
    $source = [IO.Path]::GetFullPath($Mount.Source); Assert-NoReparse $source
    $prefix = $State.Root.TrimEnd('\') + '\'
    if ($source.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
      $relative = $source.Substring($prefix.Length).Replace('\','/')
      # Operational output/temp binds retain their exact source and mode even
      # when a tracked placeholder happens to exist below them. Never restore them.
      $operational=$relative -cin @('output','temp') -and $Mount.Destination -ceq "/workspace/$relative"
      if (-not $operational -and ($Tracked -ccontains $relative -or @($Tracked | Where-Object { $_.StartsWith($relative + '/', [StringComparison]::Ordinal) }).Count)) {
        $source = Join-Path $State.Release $relative; $result.read_only=$true
      } elseif ($relative -match '^(scripts|tools)(/|$)') { Stop-Prod 'Untracked source bind cannot enter a production release.' }
    }
    $result.source=$source; $result.bind=@{create_host_path=$false}
  } else { Stop-Prod 'Unsupported WebUI mount type.' }
  $result
}
function Get-ProdPorts($Container) {
  $ports=@()
  foreach ($binding in $Container.Raw.HostConfig.PortBindings.PSObject.Properties) {
    if ($binding.Name -cne '8080/tcp') { Stop-Prod 'Unexpected main WebUI published target.' }
    foreach ($hostBinding in $binding.Value) {
      if ($hostBinding.HostPort -cne '3000') { Stop-Prod 'Main WebUI must own host port 3000.' }
      $item=[ordered]@{target=8080;published='3000';protocol='tcp'}
      if ($hostBinding.HostIp) { $item.host_ip=$hostBinding.HostIp }
      $ports += $item
    }
  }
  if (-not $ports.Count) { Stop-Prod 'No existing main WebUI port-3000 exposure was captured.' }
  $ports | Sort-Object { [string]$_.host_ip }
}
function Write-ProdOverride($State, $Ui, $Knowledge, [string[]]$Tracked) {
  $uiMounts=@($Ui.Record.Mounts | ForEach-Object { Convert-ProdMount $_ $State $Tracked })
  foreach ($mount in $uiMounts) {
    if ($mount.target -in @('/workspace/tools/analyze_document_production.py','/run/secrets/document_planner_prod_token')) { Stop-Prod 'A reserved production mount is already occupied.' }
  }
  $ui=[ordered]@{ports=@(Get-ProdPorts $Ui);volumes=$uiMounts;read_only=[bool]$Ui.Raw.HostConfig.ReadonlyRootfs}
  foreach ($pair in @(@('Memory','mem_limit'),@('MemorySwap','memswap_limit'),@('PidsLimit','pids_limit'),@('ShmSize','shm_size'))) {
    $value=Get-Field $Ui.Raw.HostConfig $pair[0]
    if ($null -ne $value -and $value -gt 0) { $ui[$pair[1]]=$value }
  }
  if ($Ui.Raw.HostConfig.NanoCpus) { $ui.cpus=$Ui.Raw.HostConfig.NanoCpus / 1000000000 }
  if ($Ui.Raw.HostConfig.CpuQuota -gt 0 -or $Ui.Raw.HostConfig.CpuShares -gt 0) { Stop-Prod 'Custom CPU scheduling requires explicit review.' }
  foreach ($pair in @(@('CapAdd','cap_add'),@('CapDrop','cap_drop'),@('SecurityOpt','security_opt'),@('Tmpfs','tmpfs'))) {
    $value=Get-Field $Ui.Raw.HostConfig $pair[0]
    if ($value) { $ui[$pair[1]]=$value }
  }
  $knowledgeMounts=@($Knowledge.Record.Mounts | Where-Object { $_.Destination -ne '/app/server.py' } | ForEach-Object { Convert-ProdMount $_ $State $Tracked })
  $services=[ordered]@{'open-webui'=$ui;'knowledge-service'=@{volumes=$knowledgeMounts};'document-planner-prod'=@{}}
  foreach ($name in @($services.Keys)) { $services[$name].labels=@{'io.agent-production.run'=$State.RunId;'io.agent-production.commit'=$State.Commit} }
  $volumes=[ordered]@{}
  foreach ($mount in $uiMounts) { if ($mount.type -eq 'volume') { $volumes[$mount.source]=@{external=$true;name=$mount.source} } }
  Write-PrivateText $State.Override (ConvertTo-Json @{services=$services;volumes=$volumes} -Depth 30)
}
function Assert-Storage($State) {
  $volume=Read-DockerJson @('volume','inspect','--format','{{json .}}','open-webui')
  if ($volume.Name -cne 'open-webui' -or $volume.Driver -cne 'local' -or
      ($volume.Options -and @($volume.Options.PSObject.Properties).Count)) { Stop-Prod 'Unsupported external/remote WebUI volume storage.' }
  if ($State.VolumeHash -and (Get-ObjectHash $volume) -cne $State.VolumeHash) { Stop-Prod 'WebUI volume was replaced or reconfigured.' }
  $db=Assert-ChildPath $State.DatabasePath $State.Root
  if ($db -ine (Join-Path $State.Root 'knowledge-db')) { Stop-Prod 'Refusing access to an unapproved database path.' }
  Assert-NoReparse $db -Tree
  if (-not (Test-Path -LiteralPath $db -PathType Container)) { Stop-Prod 'Existing Chroma bind directory is missing.' }
  $null=Assert-Original $State 'chromadb'
  $volume
}
function Invoke-Helper($State, [string[]]$Mounts, [string]$Code) {
  $images=Get-ProdImages
  $cached=Read-DockerJson @('image','inspect','--format','{{json .Id}}',$images.helper)
  if ($cached -cne $images.helper) { Stop-Prod 'Cached backup helper is unavailable; no pulls are permitted.' }
  # Tar must read all owners and restore original ownership/modes. These narrow
  # filesystem capabilities cannot make a read-only mount/root writable.
  # PowerShell stdin can carry CRLF (including an appended final newline).
  # Normalize bytes inside the cached image for EVERY helper shell operation.
  $bootstrap=@'
import subprocess,sys
script=sys.stdin.buffer.read().replace(b'\r\n',b'\n').replace(b'\r',b'\n')
sys.exit(subprocess.run(['sh','-s'],input=script).returncode)
'@
  $args=@('run','--rm','-i','--pull','never','--network','none','--read-only','--user','0:0','--cap-drop','ALL',
    '--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER','--cap-add','FSETID','--security-opt','no-new-privileges:true',
    '--pids-limit','64','--tmpfs','/tmp:rw,size=16m','--entrypoint','python')
  foreach ($mount in $Mounts) { $args+=@('--mount',$mount) }
  $args+=@($images.helper,'-B','-c',$bootstrap)
  Invoke-ProdNative Docker $args 'isolated cached backup helper' -InputText ("set -eu`n" + $Code)
}
function Assert-HelperCapabilities($State) {
  # All external commands used by archive creation, validation AND restoration.
  try { $null=Invoke-Helper $State @() 'for tool in tar find sha256sum sort cmp rm; do command -v "$tool" >/dev/null || exit 1; done' }
  catch { Stop-Prod 'Cached helper must provide sh, tar, find, sha256sum, sort, cmp and rm before quiescing production; restore the reviewed helper capabilities offline.' }
}
function Get-DataMount($State, [ValidateSet('webui','chroma')][string]$Target, [switch]$Writable) {
  $null=Assert-Storage $State
  $mount=if ($Target -eq 'webui') { 'type=volume,source=open-webui,target=/source' } else { "type=bind,source=$($State.DatabasePath),target=/source" }
  if (-not $Writable) { $mount+=',readonly' }
  $mount
}
function Assert-ArchiveListing([string[]]$Names, [string[]]$Kinds) {
  if (-not $Names.Count -or $Names.Count -ne $Kinds.Count) { Stop-Prod 'Archive listing is incomplete or contains unsupported names.' }
  $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  for ($index=0;$index -lt $Names.Count;$index++) {
    $name=$Names[$index]
    if (-not $name -or $name.StartsWith('/') -or $name -match '(^|/)\.\.(/|$)|[\\:\x00-\x1f]' -or
        $Kinds[$index] -cnotmatch '^[-d]' -or -not $seen.Add($name.TrimEnd('/'))) {
      Stop-Prod 'Unsafe archive: absolute/traversing/duplicate paths, links, or special entries are refused.'
    }
  }
}
function Read-ArchiveMetadata($State, [string]$Path, [switch]$AllowEmpty) {
  $path=Assert-ChildPath $Path $State.Directory; Assert-NoReparse $path
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Prod 'Backup/export archive is missing.' }
  $tag=[guid]::NewGuid().ToString('N'); $leaf=[IO.Path]::GetFileName($path)
  if($leaf -cnotmatch '^(release|cold-(webui|chroma)|failed-[a-f0-9]{32}-(webui|chroma))\.tar$'){Stop-Prod 'Unapproved archive filename.'}
  $code="tar -tf /archive/$leaf > /out/$tag.names`ntar -tvf /archive/$leaf > /out/$tag.kinds"
  $null=Invoke-Helper $State @("type=bind,source=$($State.Directory),target=/archive,readonly","type=bind,source=$($State.Directory),target=/out") $code
  $namesPath=Join-Path $State.Directory "$tag.names"; $kindsPath=Join-Path $State.Directory "$tag.kinds"
  foreach ($file in @($path,$namesPath,$kindsPath)) { Protect-ProdPath $file }
  if ((Get-Item $namesPath).Length -gt 64MB -or (Get-Item $kindsPath).Length -gt 64MB) { Stop-Prod 'Archive inventory exceeds the reviewed bound.' }
  $names=[IO.File]::ReadAllLines($namesPath,[Text.UTF8Encoding]::new($false,$true))
  $kinds=[IO.File]::ReadAllLines($kindsPath,[Text.UTF8Encoding]::new($false,$true))
  Assert-ArchiveListing $names $kinds
  $files=@($kinds | Where-Object { $_.StartsWith('-') }).Count
  if (-not $AllowEmpty -and $files -eq 0) { Stop-Prod 'Empty archive cannot establish a production baseline.' }
  [pscustomobject]@{Path=$path;Hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash;NamesHash=(Get-FileHash $namesPath -Algorithm SHA256).Hash;
    Files=$files;Names=@($names);Bytes=(Get-Item $path).Length}
}
function Assert-Archive($State, $Archive) {
  if(-not $Archive){Stop-Prod 'Missing verified archive.'}
  $null=Assert-ChildPath $Archive.Path $State.Directory;Assert-NoReparse $Archive.Path
  if ((Get-FileHash -LiteralPath $Archive.Path -Algorithm SHA256).Hash -cne $Archive.Hash) { Stop-Prod 'Archive hash mismatch; restoration/cutover is blocked.' }
  $check=Read-ArchiveMetadata $State $Archive.Path -AllowEmpty
  if ($check.NamesHash -cne $Archive.NamesHash -or $check.Files -ne $Archive.Files) { Stop-Prod 'Archive contents changed or are incomplete.' }
  if($Archive.Manifest){$null=Assert-ChildPath $Archive.Manifest $State.Directory;Assert-NoReparse $Archive.Manifest
    if((Get-FileHash -LiteralPath $Archive.Manifest -Algorithm SHA256).Hash -cne $Archive.ManifestHash){Stop-Prod 'Backup file inventory hash mismatch.'}}
}
function Assert-TarFileHashes($State,$Archive) {
  # Independently compare the archived bytes to the quiescent source inventory.
  # Stream uncompressed tar; no extra extracted copy or GNU-only tar flags.
  $null=Assert-ChildPath $Archive.Path $State.Directory;$null=Assert-ChildPath $Archive.Manifest $State.Directory
  $expected=[Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
  foreach($line in [IO.File]::ReadAllLines($Archive.Manifest,[Text.UTF8Encoding]::new($false,$true))){
    if($line -cnotmatch '^([a-f0-9]{64})  (\./.+)$' -or $expected.ContainsKey($Matches[2])){Stop-Prod 'Invalid archive source inventory.'}
    $expected.Add($Matches[2],$Matches[1])
  }
  $stream=[IO.File]::OpenRead($Archive.Path);$header=New-Object byte[] 512;$buffer=New-Object byte[] 1048576
  $index=0;$files=0
  try{
    while($stream.Position -lt $stream.Length){
      if($stream.Read($header,0,512) -ne 512){Stop-Prod 'Truncated tar header.'}
      if($header[0] -eq 0){break}
      if($header[124] -band 128){
        [long]$size=$header[124] -band 127
        for($n=125;$n -lt 136;$n++){
          if($size -gt ([long]::MaxValue-$header[$n])/256){Stop-Prod 'Unsupported archive size.'}
          $size=$size*256+$header[$n]
        }
      }else{
        $octal=[Text.Encoding]::ASCII.GetString($header,124,12).Trim([char]0,[char]32)
        if($octal -and $octal -cnotmatch '^[0-7]+$'){Stop-Prod 'Unsupported tar size encoding.'}
        [long]$size=if($octal){[Convert]::ToInt64($octal,8)}else{0}
      }
      [long]$padding=(512-($size%512))%512
      if($size -lt 0 -or $size+$padding -gt $stream.Length-$stream.Position){Stop-Prod 'Truncated tar payload.'}
      $type=$header[156]
      if($type -in @(76,75,120,103)){$stream.Position+=$size+$padding;continue} # GNU/PAX metadata, not file entries.
      if($index -ge $Archive.Names.Count -or $type -notin @(0,48,53,55)){Stop-Prod 'Archive entry type/inventory mismatch.'}
      $name=$Archive.Names[$index];$index++
      if($type -eq 53){$stream.Position+=$size+$padding;continue}
      if(-not $expected.ContainsKey($name)){Stop-Prod 'Archive contains an unexpected file.'}
      $sha=[Security.Cryptography.SHA256]::Create()
      try{
        [long]$remaining=$size
        while($remaining -gt 0){$read=$stream.Read($buffer,0,[int][Math]::Min($remaining,$buffer.Length));if($read -le 0){Stop-Prod 'Truncated archived file.'}
          $null=$sha.TransformBlock($buffer,0,$read,$buffer,0);$remaining-=$read}
        $null=$sha.TransformFinalBlock([byte[]]@(),0,0)
        $actual=([BitConverter]::ToString($sha.Hash)).Replace('-','').ToLowerInvariant()
        if($actual -cne $expected[$name]){Stop-Prod 'Archived file contents differ from the cold source hashes.'}
      }finally{$sha.Dispose()}
      $stream.Position+=$padding;$files++
    }
    if($index -ne $Archive.Names.Count -or $files -ne $expected.Count){Stop-Prod 'Archive omitted source entries.'}
  }finally{$stream.Dispose()}
}
function Assert-Quiescent($State) {
  foreach ($name in $State.Originals.PSObject.Properties.Name) {
    $current=Assert-Original $State $name
    if ($current.Raw.State.Running -or (Get-RestartArgument $current.Record.RestartPolicy) -cne 'no') { Stop-Prod 'All original writers, including Chroma and historical duplicates, must be stopped and restart-disabled for cold backup.' }
  }
  foreach ($name in $State.Replacements.PSObject.Properties.Name) {
    if(@(Get-Field $State 'RemovedIds' @()) -ccontains $State.Replacements.$name){continue}
    $current=Get-Replacement $State $name
    if ($current.Raw.State.Running) { Stop-Prod 'Replacement writer is still running.' }
  }
  Assert-NoUnknownWriters $State
}
function New-ColdBackup($State, [string]$Target, [string]$Prefix, [switch]$AllowEmpty) {
  Assert-Quiescent $State
  $mount=Get-DataMount $State $Target
  $leaf="$Prefix-$Target.tar"; $manifest="$Prefix-$Target.files"
  $code="cd /source`nfind . ! -type f ! -type d -print > /out/unsupported`nfind . -type f -links +1 -print >> /out/unsupported`ntest ! -s /out/unsupported`ntar -cpf /out/$leaf .`nfind . -type f -exec sha256sum {} \; | LC_ALL=C sort > /out/$manifest"
  $null=Invoke-Helper $State @($mount,"type=bind,source=$($State.Directory),target=/out") $code
  $archive=Read-ArchiveMetadata $State (Join-Path $State.Directory $leaf) -AllowEmpty:$AllowEmpty
  $manifestPath=Join-Path $State.Directory $manifest; Protect-ProdPath $manifestPath
  $fileLines=[IO.File]::ReadAllLines($manifestPath,[Text.UTF8Encoding]::new($false,$true))
  if ($fileLines.Count -ne $archive.Files -or @($fileLines | Where-Object { $_ -cnotmatch '^[a-f0-9]{64}  \./' }).Count) { Stop-Prod 'Cold-backup file inventory is incomplete.' }
  $archive | Add-Member -NotePropertyName Manifest -NotePropertyValue $manifestPath
  $archive | Add-Member -NotePropertyName ManifestHash -NotePropertyValue (Get-FileHash $manifestPath -Algorithm SHA256).Hash
  Assert-TarFileHashes $State $archive
  if (-not $AllowEmpty) {
    $required=if($Target -eq 'webui'){'./webui.db'}else{'./chroma.sqlite3'}
    if ($archive.Names -cnotcontains $required) { Stop-Prod 'Expected production SQLite database is missing from the cold archive.' }
  }
  $archive
}

function Get-EnvHash($Entries) {
  $map=Convert-EnvEntries $Entries; [string[]]$keys=@($map.Keys); [Array]::Sort($keys,[StringComparer]::Ordinal)
  $ordered=[ordered]@{}; foreach($key in $keys){$ordered[$key]=$map[$key]}; Get-ObjectHash $ordered
}
function Assert-NoUnknownWriters($State) {
  $known=@($State.Originals.PSObject.Properties.Value.Id)+@($State.Replacements.PSObject.Properties.Value)
  $ids=Invoke-ProdNative Docker @('container','ls','-q','--no-trunc') 'writer and port ownership check'
  foreach($id in @($ids -split '\r?\n' | Where-Object {$_})) {
    Assert-Id $id; $current=Read-ProdContainer $id
    $writes=@($current.Record.Mounts | Where-Object {($_.Type -eq 'volume' -and $_.Name -ceq 'open-webui') -or
      ($_.Type -eq 'bind' -and [IO.Path]::GetFullPath($_.Source) -ieq $State.DatabasePath) }).Count -gt 0
    if (@($current.Environment | Where-Object {$_ -ceq 'CHROMA_HOST=chromadb'}).Count) {$writes=$true}
    if ($writes -and $known -cnotcontains $id) {Stop-Prod "Unknown production data writer $id; resolve with its owner. No unknown container will be stopped."}
    foreach($binding in $current.Raw.HostConfig.PortBindings.PSObject.Properties) {
      if (@($binding.Value | Where-Object {$_.HostPort -eq '3000'}).Count -and
          $id -cnotin @($State.Originals.'open-webui'.Id,(Get-Field $State.Replacements 'open-webui'))) {Stop-Prod "Unexpected port-3000 owner $id; do not cut over."}
    }
  }
}
function Invoke-ContainerProbe([string]$Id, [string]$Code) {
  Assert-Id $Id
  $raw=Invoke-ProdNative Docker @('exec','-i',$Id,'python','-B','-') 'bounded production readiness probe' -InputText $Code
  try {ConvertFrom-Json $raw -ErrorAction Stop} catch {Stop-Prod 'Readiness probe did not return bounded metadata.'}
}
function Get-UiBaseline([string]$Id) {
  Invoke-ContainerProbe $Id @'
import json, sqlite3, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=15) as response:
    assert response.status == 200 and len(response.read(65537)) <= 65536
db=sqlite3.connect('file:/app/backend/data/webui.db?mode=ro', uri=True, timeout=10)
try:
    users=db.execute('select count(*) from "user"').fetchone()[0]
    chats=db.execute('select count(*) from "chat"').fetchone()[0]
    assert users > 0
    print(json.dumps(dict(users=users,chats=chats)))
finally:
    db.close()
'@
}
function Get-KnowledgeBaseline([string]$Id, [switch]$Planner) {
  $code=@'
import hashlib,json,os,urllib.request,urllib.error
from pathlib import Path
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): raise RuntimeError('redirect refused')
opener=urllib.request.build_opener(NoRedirect)
planner=PLANNER_FLAG
token=Path('/run/secrets/agent_knowledge_token').read_text().strip() if planner else os.environ['KNOWLEDGE_TOKEN']
base='http://knowledge-service:8002' if planner else 'http://127.0.0.1:8002'
def get(path,auth=False):
    req=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token} if auth else {})
    with opener.open(req,timeout=15) as r:
        assert r.status==200
        raw=r.read(65537); assert len(raw)<=65536
    return json.loads(raw)
MAX_INVENTORY=10000; PAGE=50
def count(value):
    assert type(value) is int and 0<value<=MAX_INVENTORY
    return value
def identity(document_id,root,source):
    assert type(root) is str and root in ('input','knowledge')
    assert type(source) is str and source and source==source.strip() and len(source.encode('utf-8'))<=512
    assert '\\' not in source and ':' not in source
    assert not any(ord(c)<32 or 127<=ord(c)<=159 for c in source)
    assert not any(part in ('','.','..') for part in source.split('/'))
    assert type(document_id) is str and document_id==hashlib.sha256((root+':'+source).encode()).hexdigest()
    return document_id
def catalog(first):
    ids=[]; offset=0; total=None; page=first
    while True:
        assert type(page) is dict and page['status']=='success' and type(page['documents']) is list
        rows=page['documents']; p=page['pagination']
        assert type(p) is dict and set(p)=={'limit','offset','total','has_more','next_offset'}
        current=count(p['total'])
        if total is None: total=current
        assert current==total and type(p['limit']) is int and p['limit']==PAGE
        assert type(p['offset']) is int and p['offset']==offset
        assert type(page['result_count']) is int and page['result_count']==len(rows)==min(PAGE,total-offset)
        for row in rows:
            assert type(row) is dict and set(row)=={'document_id','folder','source'}
            ids.append(identity(row['document_id'],row['folder'],row['source']))
        offset+=len(rows); more=offset<total
        assert type(p['has_more']) is bool and p['has_more']==more
        assert (type(p['next_offset']) is int and p['next_offset']==offset) if more else p['next_offset'] is None
        if not more: return ids
        page=get('/documents?limit=50&offset='+str(offset),True)
def legacy_inventory():
    # Existing collection only. No server import, create, migration or record writes.
    assert os.environ['CHROMA_HOST']=='chromadb' and os.environ['CHROMA_PORT']=='8000'
    import chromadb
    from chromadb.config import Settings
    client=chromadb.HttpClient(host='chromadb',port=8000,settings=Settings(anonymized_telemetry=False))
    collection=client.get_collection(name='agent_file_state')
    total=count(collection.count()); ids=[]
    for offset in range(0,total,PAGE):
        assert count(collection.count())==total
        page=collection.get(limit=PAGE,offset=offset,include=['metadatas'])
        assert type(page) is dict and len(json.dumps(page).encode())<=65536
        keys=page['ids']; metadata=page['metadatas']
        assert type(keys) is list and type(metadata) is list and len(keys)==len(metadata)==min(PAGE,total-offset)
        for key,record in zip(keys,metadata):
            assert type(record) is dict
            ids.append(identity(key,record['root'],record['source']))
    assert count(collection.count())==total
    return ids
health=get('/health'); assert health['status']=='ok'
assert type(health['chunks']) is int and health['chunks']>0
count(health['indexed_files'])
# Only an original's FIRST authenticated catalog request may enable legacy mode.
# Authentication, transport/JSON errors and failures on later pages always abort.
try: first=get('/documents?limit=50&offset=0',True)
except urllib.error.HTTPError as error:
    if planner or error.code!=404: raise
    error.close()
    ids=legacy_inventory()
else: ids=catalog(first)
assert len(ids)==health['indexed_files'] and len(ids)==len(set(ids)) and len(ids)>0
print(json.dumps(dict(chunks=health['chunks'],files=health['indexed_files'],documents=len(ids),identities=hashlib.sha256('\n'.join(sorted(ids)).encode()).hexdigest())))
'@
  Invoke-ContainerProbe $Id ($code.Replace('PLANNER_FLAG',$(if($Planner){'True'}else{'False'})))
}
function Assert-BackupsDormant($State) {
  foreach($name in @('open-webui','knowledge-service','open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')) {
    $current=Assert-Original $State $name
    if($current.Raw.State.Running -or (Get-RestartArgument $current.Record.RestartPolicy) -cne 'no'){Stop-Prod "Original/backup $name must remain stopped with restart disabled."}
  }
}
function Get-Replacement($State,[string]$Name,[switch]$Register) {
  if($Name -cnotin @('open-webui','knowledge-service','document-planner-prod')){Stop-Prod 'Unapproved replacement name.'}
  $id=Find-ProdContainer $Name
  if(-not $id){Stop-Prod "Expected replacement $Name is missing."}
  $current=Read-ProdContainer $id; $labels=$current.Raw.Config.Labels
  if($State.Originals.PSObject.Properties.Value.Id -ccontains $id -or $current.Record.Name -cne $Name -or
    (Get-Field $labels 'com.docker.compose.project') -cne 'agent-production' -or (Get-Field $labels 'com.docker.compose.service') -cne $Name -or
    (Get-Field $labels 'io.agent-production.run') -cne $State.RunId -or (Get-Field $labels 'io.agent-production.commit') -cne $State.Commit){Stop-Prod "Unknown owner of replacement name $Name ($id); never stop or overwrite it."}
  $known=Get-Field $State.Replacements $Name
  if($known -and $known -cne $id){Stop-Prod "Replacement identity changed for $Name."}
  $fingerprint=Get-Field $State.ReplacementFingerprints $Name
  if($fingerprint -and $fingerprint -cne $current.Record.Fingerprint){Stop-Prod 'Recorded replacement configuration changed.'}
  if(-not $known){if(-not $Register){Stop-Prod 'Unrecorded replacement identity.'};$State.Replacements | Add-Member -NotePropertyName $Name -NotePropertyValue $id
    $State.ReplacementFingerprints | Add-Member -NotePropertyName $Name -NotePropertyValue $current.Record.Fingerprint;Save-ProdState $State}
  $current
}
function Register-Replacements($State) {
  foreach($name in @('open-webui','knowledge-service','document-planner-prod')) {
    $id=Find-ProdContainer $name
    if($id -and $State.Originals.PSObject.Properties.Value.Id -cnotcontains $id){$null=Get-Replacement $State $name -Register}
  }
}
function Assert-AliasOwners($State) {
  $targets=[ordered]@{'open-webui'=(Get-Field $State.Replacements 'open-webui');'knowledge-service'=(Get-Field $State.Replacements 'knowledge-service');
    'document-planner-prod'=(Get-Field $State.Replacements 'document-planner-prod');chromadb=$State.Originals.chromadb.Id}
  $addresses=@{}
  foreach($alias in $targets.Keys){
    Assert-Id $targets[$alias];$target=Read-ProdContainer $targets[$alias]
    if(-not $target.Raw.State.Running){Stop-Prod "Intended alias owner $alias is not running."}
    $addresses[$alias]=@()
    foreach($network in @('agent-internal','agent-production-egress')){
      $attachment=Get-Field $target.Raw.NetworkSettings.Networks $network
      if($alias -ne 'chromadb' -and -not $attachment){Stop-Prod 'Replacement is missing a production network.'}
      if($attachment){foreach($ip in @($attachment.IPAddress,$attachment.GlobalIPv6Address)){if($ip){$addresses[$alias]+=[Net.IPAddress]::Parse($ip).ToString()}}}
    }
    if(-not $addresses[$alias].Count){Stop-Prod 'Intended alias owner has no inspected IP.'}
  }
  foreach($network in @('agent-internal','agent-production-egress')){
    $members=Read-DockerJson @('network','inspect','--format','{{json .Containers}}',$network)
    foreach($member in $members.PSObject.Properties){
      $peer=Read-ProdContainer $member.Name;if(-not $peer.Raw.State.Running){continue}
      $attachment=Get-Field $peer.Raw.NetworkSettings.Networks $network
      $names=@($attachment.Aliases)+@(Get-Field $attachment 'DNSNames')+@($peer.Record.Name)
      foreach($alias in $targets.Keys){if($names -contains $alias -and $peer.Record.Id -cne $targets[$alias]){Stop-Prod "DNS alias collision: $alias on $network, owner $($peer.Record.Id). Resolve manually; no unknown container is stopped."}}
    }
  }
  foreach($name in @('open-webui','knowledge-service','document-planner-prod')){
    $current=Get-Replacement $State $name
    $answers=Invoke-ContainerProbe $current.Record.Id @'
import json,socket
print(json.dumps({name:sorted({x[4][0] for x in socket.getaddrinfo(name,None,socket.AF_UNSPEC,socket.SOCK_STREAM)}) for name in ['open-webui','knowledge-service','document-planner-prod','chromadb']}))
'@
    foreach($alias in $targets.Keys){$ips=@(Get-Field $answers $alias);if(-not $ips.Count){Stop-Prod 'DNS returned no addresses.'};foreach($ip in $ips){if($addresses[$alias] -notcontains [Net.IPAddress]::Parse($ip).ToString()){Stop-Prod "DNS answers for $alias differ from the intended current IPs; halt acceptance."}}}
  }
}
function Assert-ReplacementConfiguration($State) {
  $images=Get-ProdImages; $roles=@{'open-webui'='webui';'knowledge-service'='knowledge';'document-planner-prod'='planner'}
  foreach($name in $roles.Keys){
    $current=Get-Replacement $State $name
    if(-not $current.Raw.State.Running -or $current.Record.Image -cne $images[$roles[$name]] -or
      (Get-RestartArgument $current.Record.RestartPolicy) -cne 'no'){Stop-Prod 'Replacement image/state/restart policy mismatch.'}
    $expected=@($State.ExpectedMounts.$name)
    if($current.Record.Mounts.Count -ne $expected.Count){Stop-Prod 'Replacement mount count changed.'}
    foreach($mount in $expected){
      $actual=@($current.Record.Mounts | Where-Object {$_.Destination -ceq $mount.target})
      if($actual.Count -ne 1 -or $actual[0].Type -cne $mount.type -or [bool]$actual[0].RW -eq [bool]$mount.read_only){Stop-Prod 'Replacement mount type/mode mismatch.'}
      $source=if($mount.type -eq 'volume'){$actual[0].Name}else{$actual[0].Source}
      if($mount.type -eq 'bind'){if([IO.Path]::GetFullPath($source) -ine [IO.Path]::GetFullPath($mount.source)){Stop-Prod 'Replacement bind source is not the pinned source.'}}
      elseif($source -cne $mount.source){Stop-Prod 'Replacement volume identity mismatch.'}
    }
    if($name -eq 'open-webui'){
      if((Get-ObjectHash @(Get-ProdPorts $current)) -cne (Get-ObjectHash $State.UiPorts) -or (Get-EnvHash $current.Environment) -cne $State.UiEnvHash){Stop-Prod 'WebUI port exposure or environment-held settings/signing key changed.'}
      if((Get-Field $current.Raw.State 'Health') -and $current.Raw.State.Health.Status -cne 'healthy'){Stop-Prod 'WebUI healthcheck is not healthy.'}
    } elseif($current.Raw.HostConfig.PortBindings -and @($current.Raw.HostConfig.PortBindings.PSObject.Properties).Count){Stop-Prod 'Knowledge/API ports must not be published.'}
    if($name -eq 'knowledge-service' -and (Get-EnvHash $current.Environment) -cne $State.KnowledgeEnvHash){Stop-Prod 'Knowledge environment changed.'}
  }
}
function Assert-ProductionReady($State) {
  Assert-BackupsDormant $State;Assert-ReplacementConfiguration $State;Assert-AliasOwners $State
  $ui=Get-UiBaseline (Get-Field $State.Replacements 'open-webui')
  if($ui.users -lt $State.Baseline.Ui.users -or $ui.chats -lt $State.Baseline.Ui.chats){Stop-Prod 'WebUI user/chat counts unexpectedly decreased.'}
  $data=Get-KnowledgeBaseline (Get-Field $State.Replacements 'document-planner-prod') -Planner
  if($data.documents -ne $State.Baseline.Knowledge.documents -or $data.identities -cne $State.Baseline.Knowledge.identities -or
    $data.chunks -lt $State.Baseline.Knowledge.chunks -or $data.files -lt $State.Baseline.Knowledge.files){Stop-Prod 'Production catalog/index is empty, changed, or unexpectedly reset.'}
  $check=Invoke-ContainerProbe (Get-Field $State.Replacements 'open-webui') @'
import json,urllib.request,urllib.error
from pathlib import Path
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): raise RuntimeError('redirect refused')
opener=urllib.request.build_opener(NoRedirect)
def call(url,data=None,auth=None):
    req=urllib.request.Request(url,data=data,headers={'Authorization':'Bearer '+auth,'Content-Type':'application/json'} if auth else {})
    try:
        with opener.open(req,timeout=15) as r: status=r.status; raw=r.read(98305)
    except urllib.error.HTTPError as e:
        with e: status=e.code; raw=e.read(98305)
    assert len(raw)<=98304
    return status,json.loads(raw)
base='http://document-planner-prod:8105'
status,health=call(base+'/health'); assert status==200 and health['profile']=='production' and health['pipeline_version']=='0.6i'
token=Path('/run/secrets/document_planner_prod_token').read_text().strip()
assert call(base+'/analyze-document',b'{"request":null}')[0]==401
assert call(base+'/analyze-document',b'{"request":null}',token)[0]==400
assert call('http://knowledge-service:8002/documents?limit=1&offset=0')[0]==401
with opener.open('http://host.docker.internal:1234/v1/models',timeout=15) as r: assert r.status==200 and len(r.read(1000001))<=1000000
print(json.dumps(dict(profile='production',pipeline='0.6i',auth=True)))
'@
  if($check.profile -cne 'production' -or $check.pipeline -cne '0.6i' -or $check.auth -ne $true){Stop-Prod 'Production API authentication/profile gate failed.'}
  Assert-BackupsDormant $State
}

function Get-ReleaseHash([string]$Directory) {
  Assert-NoReparse $Directory -Tree
  $rows=@(Get-ChildItem -LiteralPath $Directory -Recurse -Force -File | Sort-Object FullName | ForEach-Object {
    $_.FullName.Substring($Directory.Length).Replace('\','/') + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
  })
  if(-not $rows.Count){Stop-Prod 'Exported release is empty.'};Get-TextHash ($rows -join "`n")
}
function Assert-Release($State) {
  $expected=Join-Path (Join-Path (Join-Path $State.Root 'runtime/production/releases') $State.Commit) $State.RunId
  if([IO.Path]::GetFullPath($State.Release) -ine [IO.Path]::GetFullPath($expected) -or
    (Get-ReleaseHash $State.Release) -cne $State.ReleaseHash){Stop-Prod 'Pinned release content/path changed.'}
  foreach($file in @('webui.env','knowledge.env','agent_knowledge_token','document_planner_prod_token','compose.json','compose.yml','empty.env')){
    $path=Join-Path $State.Directory $file;Protect-ProdPath $path
    if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -cne $State.PrivateHashes.$file){Stop-Prod 'Prepared private configuration changed; do not cut over.'}
  }
}
function Export-ProdRelease($State,[string[]]$Tracked) {
  foreach($path in @('scripts/document_service_production.py','scripts/document_pipeline_v06c.py','scripts/knowledge_adapter_v04c.py',
    'tools/analyze_document_production.py','knowledge-service/server.py')){if($Tracked -cnotcontains $path){Stop-Prod 'Expected commit does not contain the reviewed production runtime.'}}
  if(@($Tracked | Where-Object {$_ -match '(^|/)(secrets|credentials)(/|$)|(^|/)[^/]*(token|secret)[^/]*\.txt$'}).Count){Stop-Prod 'Tracked credential files cannot enter an exported release.'}
  New-PrivateDirectory $State.Release
  $archive=Join-Path $State.Directory 'release.tar'
  # Binary output must go directly to a file, never through a PowerShell pipeline.
  $null=Invoke-ProdNative Git @('-C',$State.Root,'archive','--format=tar',"--output=$archive",$State.Commit) 'commit-pinned source export'
  $metadata=Read-ArchiveMetadata $State $archive
  $files=@($metadata.Names | Where-Object {-not $_.EndsWith('/')})
  if(@(Compare-Object $files $Tracked -CaseSensitive).Count){Stop-Prod 'Export differs from the complete tracked commit tree.'}
  $null=Invoke-Helper $State @("type=bind,source=$($State.Directory),target=/archive,readonly","type=bind,source=$($State.Release),target=/release") 'tar -xpf /archive/release.tar -C /release'
  foreach($entry in @(Get-ChildItem -LiteralPath $State.Release -Force -Recurse)){Protect-ProdPath $entry.FullName}
  $images=Get-ProdImages
  foreach($role in @('planner','knowledge')){
    $code=if($role -eq 'planner'){@'
import scripts.document_service_production as service
import tools.analyze_document_production as tool
assert service.pipeline.VERSION=='0.6i' and service.PROFILE=='production'
assert [x for x in vars(tool.Tools) if not x.startswith('_')]==['analyze_document']
'@}else{@'
import os,runpy
os.environ['KNOWLEDGE_TOKEN']='synthetic-import-validation-only'
runpy.run_path('/release/knowledge-service/server.py',run_name='release_import_validation')
'@}
    $null=Invoke-ProdNative Docker @('run','--rm','-i','--pull','never','--network','none','--read-only','--cap-drop','ALL',
      '--security-opt','no-new-privileges:true','--mount',"type=bind,source=$($State.Release),target=/release,readonly",'--workdir','/release',
      '--entrypoint','python',$images[$role],'-B','-') 'credential-free release import validation' -InputText $code
  }
  $State.ReleaseHash=Get-ReleaseHash $State.Release
}
function Invoke-ProdCompose($State,[string[]]$Arguments,[string]$Template=$State.Template) {
  Assert-Release $State
  if($Arguments[0] -ceq 'up'){
    Assert-BackupsDormant $State;$null=Assert-Storage $State;Assert-NoUnknownWriters $State
    foreach($role in @('knowledge','webui','planner')){
      $image=Read-DockerJson @('image','inspect','--format','{{json .Id}}',"local-agent-production/${role}:v06j")
      if($image -cne (Get-ProdImages)[$role]){Stop-Prod 'A guarded image alias changed after Prepare.'}
    }
    foreach($name in @('open-webui','knowledge-service','document-planner-prod')){if(Find-ProdContainer $name){Stop-Prod 'Replacement name became occupied before Compose; do not overwrite it.'}}
    $chroma=Assert-Original $State chromadb;if(-not $chroma.Raw.State.Running){Stop-Prod 'Original Chroma is not running.'}
  }
  $keys=@('PROD_RELEASE_DIR','PROD_STATE_DIR','PROD_WORKSPACE_DIR');$old=@{}
  foreach($key in $keys){$old[$key]=[Environment]::GetEnvironmentVariable($key,'Process')}
  try{
    $env:PROD_RELEASE_DIR=$State.Release;$env:PROD_STATE_DIR=$State.Directory;$env:PROD_WORKSPACE_DIR=$State.Root
    Invoke-ProdNative Docker (@('compose','--project-name','agent-production','--env-file',(Join-Path $State.Directory 'empty.env'),'-f',$Template,'-f',$State.Override)+$Arguments) 'guarded production Compose'
  }finally{foreach($key in $keys){[Environment]::SetEnvironmentVariable($key,$old[$key],'Process')}}
}
function Test-SyntheticCompose($State) {
  Write-PrivateText (Join-Path $State.Directory 'synthetic.env') "WEBUI_SECRET_KEY=synthetic-only`nKNOWLEDGE_TOKEN=synthetic-only`n"
  $template=Join-Path $State.Directory 'synthetic-compose.yml'
  Write-PrivateText $template ([IO.File]::ReadAllText($State.Template).Replace('/webui.env','/synthetic.env').Replace('/knowledge.env','/synthetic.env'))
  $null=Invoke-ProdCompose $State @('config','--quiet') $template
}
function Read-ProdState([string]$Path) {
  $root=Get-ProdRoot;$path=Assert-ChildPath $Path (Join-Path $root 'runtime/production/private');Assert-NoReparse $path
  Protect-ProdPath $path
  try{$state=ConvertFrom-Json ([IO.File]::ReadAllText($path,[Text.UTF8Encoding]::new($false,$true))) -ErrorAction Stop}catch{Stop-Prod 'Invalid private deployment state.'}
  if($state.Schema -ne 1 -or $state.Root -ine $root -or $state.Commit -cnotmatch '^[a-f0-9]{40}$' -or $state.RunId -cnotmatch '^\d{8}-\d{6}-[a-f0-9]{8}$' -or
    $state.Directory -ine [IO.Path]::GetDirectoryName($path) -or $state.StatePath -ine $path -or
    $state.Phase -cnotin @('Preparing','Prepared','Quiescing','ColdBackupsVerified','Renaming','Starting','ReadyForOperatorAcceptance','RollbackStopping','RestoringData','RollbackStarting','RolledBack')){Stop-Prod 'Deployment state identity/phase is invalid.'}
  $expected=Join-Path (Join-Path $root 'runtime/production/private') $state.RunId
  if($state.Directory -ine $expected -or $state.DatabasePath -ine (Join-Path $root 'knowledge-db') -or
    $state.Template -ine (Join-Path $expected 'compose.yml') -or $state.Override -ine (Join-Path $expected 'compose.json')){Stop-Prod 'Private state paths are not approved.'}
  $names=@('open-webui','knowledge-service','chromadb','open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')
  if(@(Compare-Object @($state.Originals.PSObject.Properties.Name) $names -CaseSensitive).Count){Stop-Prod 'Original recovery inventory is incomplete.'}
  foreach($name in $names){Assert-Id $state.Originals.$name.Id;$null=Get-RestartArgument $state.Originals.$name.RestartPolicy
    if($state.Originals.$name.BackupName -cne "$name-prod-backup-$($state.RunId)"){Stop-Prod 'Unexpected original backup name.'}}
  $state
}
function Invoke-ProdPrepare([string]$Commit) {
  if($Commit -cnotmatch '^[a-f0-9]{40}$'){Stop-Prod 'Prepare requires the explicit reviewed full expected Git commit.'}
  $root=Get-ProdRoot
  if((Invoke-ProdNative Docker @('compose','version','--short') 'Compose version check').TrimStart('v') -cne '5.6.1'){Stop-Prod 'Review the Compose version; this procedure targets 5.6.1 and raw env_file support.'}
  $head=Invoke-ProdNative Git @('-C',$root,'rev-parse','--verify','HEAD') 'expected commit check'
  if($head -cne $Commit){Stop-Prod 'Expected commit is not current HEAD.'}
  $null=Invoke-ProdNative Git @('-C',$root,'diff','--quiet') 'tracked working-tree cleanliness (commit reviewed changes first)'
  $null=Invoke-ProdNative Git @('-C',$root,'diff','--cached','--quiet') 'tracked index cleanliness (commit reviewed changes first)'
  $tracked=@((Invoke-ProdNative Git @('-C',$root,'-c','core.quotepath=false','ls-tree','-r','--name-only',$Commit) 'tracked runtime inventory') -split '\r?\n' | Where-Object {$_})
  if(Invoke-ProdNative Docker @('container','ls','-aq','--filter','label=com.docker.compose.project=agent-production') 'project vacancy check'){Stop-Prod 'agent-production already has containers; use its recorded state, not a new Prepare.'}
  if(Find-ProdContainer 'document-planner-prod'){Stop-Prod 'Production API name is occupied.'}
  $internal=Read-DockerJson @('network','inspect','--format','{{json .}}','agent-internal')
  if($internal.Name -cne 'agent-internal'){Stop-Prod 'Existing internal network is missing.'}
  $egress=Invoke-ProdNative Docker @('network','ls','-q','--no-trunc','--filter','name=^agent-production-egress$') 'production egress prerequisite'
  if($egress){$network=Read-DockerJson @('network','inspect','--format','{{json .}}','agent-production-egress')
    if((Get-Field $network.Labels 'io.agent-production.project') -cne 'agent-production' -or ($network.Containers -and @($network.Containers.PSObject.Properties).Count)){Stop-Prod 'Production egress network is occupied or not identified as production-owned.'}}
  $records=[ordered]@{};$privateRecords=@{}
  $roles=@{'open-webui'='webui';'knowledge-service'='knowledge'};$images=Get-ProdImages
  foreach($name in @('open-webui','knowledge-service','chromadb','open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')){
    $id=Find-ProdContainer $name;if(-not $id){Stop-Prod "Expected original $name is missing."}
    $current=Read-ProdContainer $id
    if($current.Record.Name -cne $name -or (Get-Field $current.Raw.Config.Labels 'com.docker.compose.project')){Stop-Prod 'Original name/project differs from the unlabelled baseline.'}
    $null=Get-RestartArgument $current.Record.RestartPolicy
    if($roles.ContainsKey($name) -and $current.Record.Image -cne $images[$roles[$name]]){Stop-Prod 'Canonical production image differs from its reviewed cached ID.'}
    if((Read-DockerJson @('image','inspect','--format','{{json .Id}}',$current.Record.Image)) -cne $current.Record.Image){Stop-Prod 'Original image is not cached.'}
    $records[$name]=$current.Record;$privateRecords[$name]=$current
  }
  Assert-MainMounts $privateRecords['open-webui'] $privateRecords['knowledge-service'] $privateRecords['chromadb'] $root
  foreach($name in @('open-webui-before-scripts','open-webui-backup')){if(-not @($records[$name].Mounts | Where-Object {$_.Type -eq 'volume' -and $_.Name -ceq 'open-webui'}).Count){Stop-Prod 'Historical WebUI identity does not share the expected production volume.'}}
  if($privateRecords['knowledge-service-v1-backup'].Environment -cnotcontains 'CHROMA_HOST=chromadb'){Stop-Prod 'Historical knowledge target differs from the observed baseline.'}
  $uiEnv=Convert-EnvEntries $privateRecords['open-webui'].Environment;$knowledgeEnv=Convert-EnvEntries $privateRecords['knowledge-service'].Environment
  Assert-DatabaseConfiguration $uiEnv $knowledgeEnv (Convert-EnvEntries $privateRecords['chromadb'].Environment)
  $credentialPath=Join-Path $root 'knowledge-service/knowledge-token.txt';Assert-NoReparse $credentialPath
  $credential=[IO.File]::ReadAllText($credentialPath,[Text.UTF8Encoding]::new($false,$true)).Trim()
  if(-not $credential -or -not $knowledgeEnv.ContainsKey('KNOWLEDGE_TOKEN') -or $credential -cne $knowledgeEnv['KNOWLEDGE_TOKEN']){Stop-Prod 'Existing knowledge credential does not match the service environment.'}
  $run=(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
  foreach($name in @($records.Keys)){$records[$name].BackupName="$name-prod-backup-$run";if(Find-ProdContainer $records[$name].BackupName){Stop-Prod 'Unique backup name is occupied.'}}
  $base=Join-Path $root 'runtime';New-PrivateDirectory $base
  foreach($part in @('production','production/private','production/releases')){New-PrivateDirectory (Join-Path $base $part)}
  $directory=Join-Path (Join-Path $base 'production/private') $run;New-PrivateDirectory $directory
  $releaseParent=Join-Path (Join-Path $base 'production/releases') $Commit;New-PrivateDirectory $releaseParent
  $state=[pscustomobject]@{Schema=1;RunId=$run;Commit=$Commit;Root=$root;Directory=$directory;StatePath=(Join-Path $directory 'state.json');
    Release=(Join-Path $releaseParent $run);ReleaseHash=$null;Template=(Join-Path $directory 'compose.yml');Override=(Join-Path $directory 'compose.json');
    Phase='Preparing';Originals=[pscustomobject]$records;Replacements=[pscustomobject]@{};ReplacementFingerprints=[pscustomobject]@{};RemovedIds=@();DatabasePath=(Join-Path $root 'knowledge-db');
    VolumeHash=$null;InternalNetwork=$internal.Id;EgressId=$egress;Baseline=$null;Backups=$null;FailedState=@();ExpectedMounts=$null;
    UiPorts=@(Get-ProdPorts $privateRecords['open-webui']);UiEnvHash=(Get-EnvHash $privateRecords['open-webui'].Environment);
    KnowledgeEnvHash=(Get-EnvHash $privateRecords['knowledge-service'].Environment);PrivateHashes=$null}
  Save-ProdState $state # Recovery IDs and exact restart policies precede every Docker mutation.
  $volume=Assert-Storage $state;$state.VolumeHash=Get-ObjectHash $volume
  Assert-NoUnknownWriters $state
  Write-RawEnv (Join-Path $directory 'webui.env') $privateRecords['open-webui'].Environment
  Write-RawEnv (Join-Path $directory 'knowledge.env') $privateRecords['knowledge-service'].Environment
  Write-PrivateText (Join-Path $directory 'agent_knowledge_token') $credential
  $bytes=New-Object byte[] 32;$rng=[Security.Cryptography.RandomNumberGenerator]::Create()
  try{$rng.GetBytes($bytes);Write-PrivateText (Join-Path $directory 'document_planner_prod_token') ([Convert]::ToBase64String($bytes))}finally{$rng.Dispose();[Array]::Clear($bytes,0,$bytes.Length)}
  Write-PrivateText (Join-Path $directory 'empty.env') ''
  Write-PrivateText $state.Template ([IO.File]::ReadAllText((Join-Path $root 'deployment/docker-compose.production.yml')))
  foreach($role in $images.Keys){if((Read-DockerJson @('image','inspect','--format','{{json .Id}}',$images[$role])) -cne $images[$role]){Stop-Prod 'Required cached image is unavailable; no builds/pulls are allowed.'}}
  foreach($role in @('knowledge','webui','planner')){
    $alias="local-agent-production/${role}:v06j";$tag=Invoke-ProdNative Docker @('image','ls','--no-trunc','-q',$alias) 'local alias guard'
    if($tag -and $tag -cne $images[$role]){Stop-Prod 'Existing local image alias points elsewhere; refusing to retag.'}
    if(-not $tag){$null=Invoke-ProdNative Docker @('image','tag',$images[$role],$alias) 'guarded local image alias'}
  }
  Assert-HelperCapabilities $state
  foreach($target in @('webui','chroma')){
    $mount=Get-DataMount $state $target
    $required=if($target -eq 'webui'){'webui.db'}else{'chroma.sqlite3'}
    $null=Invoke-Helper $state @($mount) "cd /source`ntest -s $required`ntest -z `"`$(find . ! -type f ! -type d -print)`"`ntest -z `"`$(find . -type f -links +1 -print)`""
  }
  Export-ProdRelease $state $tracked
  Write-ProdOverride $state $privateRecords['open-webui'] $privateRecords['knowledge-service'] $tracked
  $override=ConvertFrom-Json ([IO.File]::ReadAllText($state.Override))
  $uiMounts=@($override.services.'open-webui'.volumes)+@(
    @{type='bind';source=(Join-Path $state.Release 'tools/analyze_document_production.py');target='/workspace/tools/analyze_document_production.py';read_only=$true},
    @{type='bind';source=(Join-Path $directory 'document_planner_prod_token');target='/run/secrets/document_planner_prod_token';read_only=$true})
  $state.ExpectedMounts=[pscustomobject]@{'open-webui'=$uiMounts;'knowledge-service'=@($override.services.'knowledge-service'.volumes)+@(
      @{type='bind';source=(Join-Path $state.Release 'knowledge-service/server.py');target='/app/server.py';read_only=$true});
    'document-planner-prod'=@(@{type='bind';source=$state.Release;target='/workspace';read_only=$true},
      @{type='bind';source=(Join-Path $directory 'agent_knowledge_token');target='/run/secrets/agent_knowledge_token';read_only=$true},
      @{type='bind';source=(Join-Path $directory 'document_planner_prod_token');target='/run/secrets/document_planner_prod_token';read_only=$true})}
  $hashes=[ordered]@{};foreach($file in @('webui.env','knowledge.env','agent_knowledge_token','document_planner_prod_token','compose.json','compose.yml','empty.env')){
    $hashes[$file]=(Get-FileHash (Join-Path $directory $file) -Algorithm SHA256).Hash}
  $state.PrivateHashes=[pscustomobject]$hashes
  $state.Baseline=[pscustomobject]@{Ui=(Get-UiBaseline $state.Originals.'open-webui'.Id);Knowledge=(Get-KnowledgeBaseline $state.Originals.'knowledge-service'.Id)}
  Test-SyntheticCompose $state
  foreach($name in $state.Originals.PSObject.Properties.Name){$null=Assert-Original $state $name}
  Set-ProdPhase $state 'Prepared'
  [pscustomobject]@{Action='Prepare';Phase=$state.Phase;StatePath=$state.StatePath;Commit=$Commit}
}

function Assert-ProductionNetworks($State,[switch]$Create) {
  $internal=Read-DockerJson @('network','inspect','--format','{{json .}}','agent-internal')
  if($internal.Id -cne $State.InternalNetwork){Stop-Prod 'The external internal network was replaced.'}
  $found=Invoke-ProdNative Docker @('network','ls','-q','--no-trunc','--filter','name=^agent-production-egress$') 'egress identity check'
  if(-not $found){
    if($State.EgressId -or -not $Create){Stop-Prod 'Recorded production egress network is missing.'}
    $null=Invoke-ProdNative Docker @('network','create','--driver','bridge','--label','io.agent-production.project=agent-production',
      '--label',"io.agent-production.run=$($State.RunId)",'agent-production-egress') 'production-only egress creation'
  }
  $network=Read-DockerJson @('network','inspect','--format','{{json .}}','agent-production-egress')
  if($network.Name -cne 'agent-production-egress' -or $network.Driver -cne 'bridge' -or $network.Internal -or
    (Get-Field $network.Labels 'io.agent-production.project') -cne 'agent-production' -or
    ($State.EgressId -and $network.Id -cne $State.EgressId)){Stop-Prod 'Unexpected production egress identity/configuration.'}
  $State.EgressId=$network.Id;Save-ProdState $State
}
function Invoke-ProdCutover($State) {
  if($State.Phase -cne 'Prepared'){Stop-Prod 'Cutover requires Prepared state. For a partial failure use the recorded Rollback, not blind retries.'}
  Assert-Release $State;$null=Assert-Storage $State;Assert-NoUnknownWriters $State
  foreach($name in $State.Originals.PSObject.Properties.Name){$current=Assert-Original $State $name;if($current.Record.Name -cne $name){Stop-Prod 'An original moved after Prepare.'}}
  Assert-HelperCapabilities $State
  Set-ProdPhase $State 'Quiescing'
  # Stop all shared-volume/historical knowledge writers before canonical services.
  foreach($name in @('open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup','open-webui','knowledge-service','chromadb')){Stop-Original $State $name}
  Assert-Quiescent $State
  $State.Backups=[pscustomobject]@{webui=(New-ColdBackup $State webui 'cold');chroma=(New-ColdBackup $State chroma 'cold')}
  Assert-Archive $State $State.Backups.webui;Assert-Archive $State $State.Backups.chroma
  Set-ProdPhase $State 'ColdBackupsVerified'
  Set-ProdPhase $State 'Renaming'
  foreach($name in @('open-webui','knowledge-service')){
    $current=Assert-Original $State $name;$backup=$State.Originals.$name.BackupName
    if(Find-ProdContainer $backup){Stop-Prod 'Backup name became occupied; never overwrite it.'}
    $null=Invoke-ProdNative Docker @('container','rename',$current.Record.Id,$backup) 'verified canonical backup rename'
    $null=Assert-Original $State $name;Save-ProdState $State
  }
  Assert-BackupsDormant $State
  Start-Original $State 'chromadb'
  Assert-ProductionNetworks $State -Create
  Set-ProdPhase $State 'Starting'
  try{$null=Invoke-ProdCompose $State @('up','-d','--no-recreate','--no-build','--pull','never')}
  finally{Register-Replacements $State}
  # Readiness may need time, but failures never trigger automatic rollback/deletion.
  $ready=$false
  for($attempt=0;$attempt -lt 12;$attempt++){
    try{Assert-ProductionReady $State;$ready=$true;break}catch{if($attempt -eq 11){throw};Start-Sleep -Seconds 5}
  }
  if(-not $ready){Stop-Prod 'Readiness failed; use Status and explicit Rollback.'}
  Set-ProdPhase $State 'ReadyForOperatorAcceptance'
  [pscustomobject]@{Action='Cutover';Phase=$State.Phase;StatePath=$State.StatePath;ManualAcceptanceRequired=$true}
}
function Stop-ProdReplacements($State) {
  Register-Replacements $State
  foreach($name in $State.Replacements.PSObject.Properties.Name){
    $id=$State.Replacements.$name
    if(@($State.RemovedIds) -ccontains $id){continue}
    $present=Invoke-ProdNative Docker @('container','ls','-aq','--no-trunc','--filter',"id=$id") 'recorded replacement existence'
    if(-not $present){$State.RemovedIds+= $id;Save-ProdState $State;continue}
    $current=Get-Replacement $State $name
    $null=Invoke-ProdNative Docker @('container','update','--restart=no',$current.Record.Id) 'verified replacement restart disable'
    $current=Get-Replacement $State $name
    if($current.Raw.State.Running){$null=Invoke-ProdNative Docker @('container','stop',$current.Record.Id) 'verified replacement stop'}
    $current=Get-Replacement $State $name
    if($current.Raw.State.Running -or (Get-RestartArgument $current.Record.RestartPolicy) -cne 'no'){Stop-Prod 'Replacement is not quiescent.'}
    $null=Invoke-ProdNative Docker @('container','rm',$current.Record.Id) 'verified stopped replacement removal (without volumes)'
    $State.RemovedIds+=$id;Save-ProdState $State
  }
}
function Restore-ColdData($State,[string]$Target,$Archive) {
  Assert-Quiescent $State;Assert-Archive $State $Archive
  if([IO.Path]::GetFileName($Archive.Path) -cne "cold-$Target.tar"){Stop-Prod 'Cold archive does not match its exact restore target.'}
  if(-not $State.FailedState.Count){Stop-Prod 'Verified failed-state preservation must precede data restoration.'}
  $mount=Get-DataMount $State $Target -Writable
  $leaf=[IO.Path]::GetFileName($Archive.Path);$manifest=[IO.Path]::GetFileName($Archive.Manifest)
  if($manifest -cne "cold-$Target.files"){Stop-Prod 'Unapproved restore inventory filename.'}
  $code=@'
cd /source
# The mount, stopped writers, archive paths/types, hash and failed-state backup
# have already been verified. Delete entries only inside this exact data mount.
find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} \;
tar -xpf /archive/ARCHIVE -C /source
find . -type f -exec sha256sum {} \; | LC_ALL=C sort > /out/restored.files
cmp /out/restored.files /archive/MANIFEST
'@
  $null=Invoke-Helper $State @($mount,"type=bind,source=$($State.Directory),target=/archive,readonly","type=bind,source=$($State.Directory),target=/out") ($code.Replace('ARCHIVE',$leaf).Replace('MANIFEST',$manifest))
}
function Invoke-ProdRollback($State,[switch]$RestoreData) {
  if($RestoreData){Assert-HelperCapabilities $State}
  Set-ProdPhase $State 'RollbackStopping'
  foreach($name in @('open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')){Stop-Original $State $name}
  Stop-ProdReplacements $State
  foreach($name in @('open-webui','knowledge-service','chromadb')){Stop-Original $State $name}
  if($RestoreData){
    if(-not $State.Backups){Stop-Prod 'No complete cold backup pair exists; do not restore partial archives.'}
    Assert-Archive $State $State.Backups.webui;Assert-Archive $State $State.Backups.chroma
    $tag='failed-'+[guid]::NewGuid().ToString('N')
    $failed=[pscustomobject]@{webui=(New-ColdBackup $State webui $tag -AllowEmpty);chroma=(New-ColdBackup $State chroma $tag -AllowEmpty)}
    Assert-Archive $State $failed.webui;Assert-Archive $State $failed.chroma
    $State.FailedState+= $failed;Save-ProdState $State
    Set-ProdPhase $State 'RestoringData'
    Restore-ColdData $State webui $State.Backups.webui
    Restore-ColdData $State chroma $State.Backups.chroma
  }
  foreach($name in @('open-webui','knowledge-service')){
    $current=Assert-Original $State $name
    if($current.Record.Name -cne $name){
      if(Find-ProdContainer $name){Stop-Prod "Original name $name is occupied; never overwrite it."}
      $null=Invoke-ProdNative Docker @('container','rename',$current.Record.Id,$name) 'verified original name restoration'
      $null=Assert-Original $State $name;Save-ProdState $State
    }
  }
  Set-ProdPhase $State 'RollbackStarting'
  foreach($name in @('chromadb','knowledge-service','open-webui')){Start-Original $State $name}
  foreach($name in @('open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')){
    $check=Assert-Original $State $name;if($check.Raw.State.Running -or (Get-RestartArgument $check.Record.RestartPolicy) -cne 'no'){Stop-Prod 'Historical duplicates must remain stopped.'}
  }
  Set-ProdPhase $State 'RolledBack'
  [pscustomobject]@{Action='Rollback';Phase=$State.Phase;StatePath=$State.StatePath;DataRestored=[bool]$RestoreData}
}
function Invoke-ProdDeployment {
  param([ValidateSet('Prepare','Cutover','Status','Rollback')][string]$Action,[string]$ExpectedCommit,[string]$StatePath,[switch]$RestoreData)
  try{
    if($Action -eq 'Prepare'){if($RestoreData -or $StatePath){Stop-Prod 'Prepare accepts only the explicit expected commit.'};return Invoke-ProdPrepare $ExpectedCommit}
    if(-not $StatePath){Stop-Prod 'Supply the exact private StatePath printed by Prepare.'}
    $state=Read-ProdState $StatePath
    switch($Action){
      'Cutover' {if($RestoreData){Stop-Prod 'RestoreData is only valid for Rollback.'};Invoke-ProdCutover $state}
      'Rollback' {Invoke-ProdRollback $state -RestoreData:$RestoreData}
      'Status' {
        foreach($name in $state.Originals.PSObject.Properties.Name){$null=Assert-Original $state $name}
        [pscustomobject]@{Action='Status';Phase=$state.Phase;Commit=$state.Commit;StatePath=$state.StatePath;Replacements=@($state.Replacements.PSObject.Properties.Name);ColdBackupPairRecorded=[bool]$state.Backups}
      }
    }
  }catch{if($_.Exception.Message.StartsWith('PROD-GUARD:')){throw};Stop-Prod 'Operation failed; inspect the ACL-protected state privately. No automatic cleanup or rollback was performed.'}
}

if($MyInvocation.InvocationName -ne '.' -and $Action){Invoke-ProdDeployment -Action $Action -ExpectedCommit $ExpectedCommit -StatePath $StatePath -RestoreData:$RestoreData}
