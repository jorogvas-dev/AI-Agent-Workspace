# All orchestration tests mock Docker/Git. Optional validation runs only Compose
# config against synthetic env files, never production environments or containers.
[CmdletBinding()]param([switch]$ValidateCompose)
$ErrorActionPreference='Stop'
$helper=Join-Path $PSScriptRoot 'deploy_document_production.ps1'
$tokens=$null;$parseErrors=$null
$null=[Management.Automation.Language.Parser]::ParseFile($helper,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Deployment helper failed PowerShell parsing.'}
function docker {throw 'Real Docker operations are forbidden in mocked tests.'}
function git {throw 'Real Git operations are forbidden in mocked tests.'}
. $helper -Action Cutover # Explicit action while dot-sourcing must still do nothing.

function Assert-Test([bool]$Condition,[string]$Message){if(-not $Condition){throw "TEST: $Message"}}
function Expect-Failure([scriptblock]$Body,[string]$Pattern){
  $caught=$null;try{& $Body | Out-Null}catch{$caught=$_}
  if(-not $caught -or $caught.Exception.Message -notmatch $Pattern){throw "TEST: expected $Pattern, got $caught"}
  $caught.Exception.Message
}
function With-Mocks($Map,[scriptblock]$Body){
  $saved=@{}
  try{
    foreach($key in $Map.Keys){$command=Get-Command $key -ErrorAction SilentlyContinue
      $saved[$key]=if($command -and $command.CommandType -eq 'Function'){$command.ScriptBlock}else{$null}
      Set-Item -Path ("Function:\script:$key") -Value $Map[$key]
    }
    & $Body
  }finally{foreach($key in $Map.Keys){if($saved[$key]){Set-Item -Path ("Function:\script:$key") -Value $saved[$key]}else{Remove-Item -Path ("Function:\script:$key")}}}
}
$script:passed=0
function Test-Case([string]$Name,[scriptblock]$Body){& $Body | Out-Null;$script:passed++;Write-Output "PASS $Name"}
$scratch=Join-Path ([IO.Path]::GetTempPath()) ('production-deploy-synthetic-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $scratch
# Synthetic scratch files are deliberately retained; there is no cleanup action.

function New-TestState {
  $script:events=[Collections.Generic.List[string]]::new();$script:containers=@{}
  $script:failCompose=$false;$script:failArchive=$false;$script:failRename='';$script:failRestore=$false
  $records=[ordered]@{};$index=0
  foreach($name in @('open-webui','knowledge-service','chromadb','open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')){
    $index++;$id=$index.ToString('x64')
    $policy=[pscustomobject]@{Name=$(if($name -eq 'knowledge-service'){'on-failure'}elseif($name -eq 'chromadb'){'unless-stopped'}else{'always'});MaximumRetryCount=$(if($name -eq 'knowledge-service'){7}else{0})}
    $record=[pscustomobject]@{Id=$id;Name=$name;Image='sha256:'+('a'*64);Fingerprint="fingerprint-$index";
      RestartPolicy=$policy;Mounts=@();BackupName="$name-prod-backup-20261103-120000-12345678"}
    $records[$name]=ConvertFrom-Json (ConvertTo-Json $record -Depth 20)
    $script:containers[$id]=[pscustomobject]@{Record=$record;Environment=@();Raw=[pscustomobject]@{
      Id=$id;State=[pscustomobject]@{Running=$true};Config=[pscustomobject]@{Labels=$null};HostConfig=[pscustomobject]@{PortBindings=$null};NetworkSettings=[pscustomobject]@{Networks=[pscustomobject]@{}}}}
  }
  [pscustomobject]@{Schema=1;Root='E:\AI-Agent-Workspace';RunId='20261103-120000-12345678';Commit=('a'*40);Phase='Prepared';
    StatePath=(Join-Path $scratch 'state.json');Directory=$scratch;DatabasePath='E:\AI-Agent-Workspace\knowledge-db';Originals=[pscustomobject]$records;
    Replacements=[pscustomobject]@{};ReplacementFingerprints=[pscustomobject]@{};RemovedIds=@();Backups=$null;FailedState=@();ExpectedMounts=[pscustomobject]@{};
    Release='E:\AI-Agent-Workspace\runtime\production\releases\release';ReleaseHash=$null;InternalNetwork=('f'*64);EgressId=$null;
    Baseline=[pscustomobject]@{Ui=[pscustomobject]@{users=2;chats=3};Knowledge=[pscustomobject]@{chunks=5;files=2;documents=2;identities='baseline'}}}
}
function New-TestReplacements($State,[int]$Count=3){
  $index=100
  $roles=@{'open-webui'='webui';'knowledge-service'='knowledge';'document-planner-prod'='planner'}
  foreach($name in @('open-webui','knowledge-service','document-planner-prod') | Select-Object -First $Count){
    $index++;$id=$index.ToString('x64')
    $script:containers[$id]=[pscustomobject]@{Record=[pscustomobject]@{Id=$id;Name=$name;Image=(Get-ProdImages)[$roles[$name]];Fingerprint="new-$index";
      RestartPolicy=[pscustomobject]@{Name='no';MaximumRetryCount=0};Mounts=@()};Environment=@();Raw=[pscustomobject]@{State=[pscustomobject]@{Running=$true};
      Config=[pscustomobject]@{Labels=[pscustomobject]@{'com.docker.compose.project'='agent-production';'com.docker.compose.service'=$name;
        'io.agent-production.run'=$State.RunId;'io.agent-production.commit'=$State.Commit}};HostConfig=[pscustomobject]@{PortBindings=$null};NetworkSettings=[pscustomobject]@{Networks=[pscustomobject]@{}}}}
  }
}
function Assert-Restored($State){
  foreach($name in @('open-webui','knowledge-service','chromadb')){
    $current=$script:containers[$State.Originals.$name.Id]
    Assert-Test ($current.Record.Name -ceq $name -and $current.Raw.State.Running) 'canonical originals restored'
    Assert-Test ((Get-RestartArgument $current.Record.RestartPolicy) -ceq (Get-RestartArgument $State.Originals.$name.RestartPolicy)) 'exact captured policy restored'
  }
  foreach($name in @('open-webui-before-scripts','open-webui-backup','knowledge-service-v1-backup')){
    $current=$script:containers[$State.Originals.$name.Id]
    Assert-Test (-not $current.Raw.State.Running -and (Get-RestartArgument $current.Record.RestartPolicy) -ceq 'no') 'historical duplicates remain disabled'
  }
}
$script:orchestration=@{
  'Read-ProdContainer'={param($Reference) if(-not $script:containers.ContainsKey($Reference)){Stop-Prod 'Unknown synthetic identity.'};$script:containers[$Reference]}
  'Find-ProdContainer'={param($Name) $match=@($script:containers.Values | Where-Object {$_.Record.Name -ceq $Name});if($match.Count){$match[0].Record.Id}else{$null}}
  'Save-ProdState'={param($State) $script:events.Add('phase:'+$State.Phase)}
  'Assert-Release'={param($State) $script:events.Add('release-verified')}
  'Assert-Storage'={param($State) if($State.DatabasePath -cne 'E:\AI-Agent-Workspace\knowledge-db'){Stop-Prod 'Unapproved database path.'};[pscustomobject]@{Name='open-webui'}}
  'Assert-NoUnknownWriters'={param($State)}
  'Assert-ProductionNetworks'={param($State,[switch]$Create) $script:events.Add('production-network')}
  'New-ColdBackup'={param($State,$Target,$Prefix,[switch]$AllowEmpty)
    Assert-Quiescent $State;$script:events.Add("backup:$Prefix/$Target")
    [pscustomobject]@{Path=(Join-Path $State.Directory "$Prefix-$Target.tar");Hash='hash';NamesHash='names';Files=1;Names=@('./db');Manifest=(Join-Path $State.Directory "$Prefix-$Target.files");ManifestHash='manifest'}
  }
  'Assert-Archive'={param($State,$Archive) $script:events.Add('verify:'+([IO.Path]::GetFileName($Archive.Path)));if($script:failArchive){Stop-Prod 'Archive hash mismatch.'}}
  'Invoke-Helper'={param($State,$Mounts,$Code)
    if($Code.Contains('find . -mindepth')){
      Assert-Test ($State.FailedState.Count -gt 0) 'failed-state backup precedes extraction'
      Assert-Test ($Mounts[0] -in @('type=volume,source=open-webui,target=/source','type=bind,source=E:\AI-Agent-Workspace\knowledge-db,target=/source')) 'restore mount stays exact'
      Assert-Test ($Code.Contains('cmp /out/restored.files /archive/')) 'restore file hashes checked'
      $script:events.Add('restore-data');if($script:failRestore){Stop-Prod 'Synthetic extraction failure.'}
    }
  }
  'Invoke-ProdCompose'={param($State,$Arguments,$Template)
    Assert-Test (($Arguments -join ' ') -ceq 'up -d --no-recreate --no-build --pull never') 'no pulls/builds/recreation'
    $script:events.Add('compose-up');New-TestReplacements $State $(if($script:failCompose){1}else{3})
    if($script:failCompose){Stop-Prod 'Synthetic partial Compose failure.'}
  }
  'Assert-ProductionReady'={param($State) Assert-BackupsDormant $State;$script:events.Add('ready')}
  'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
    if($Kind -ne 'Docker'){Stop-Prod 'Unexpected synthetic Git operation.'}
    if($Arguments[0] -ne 'container'){Stop-Prod 'Unexpected synthetic Docker operation.'}
    $verb=$Arguments[1]
    if($verb -eq 'ls'){$filter=$Arguments[-1];if($filter -match '^id=(.+)$' -and $script:containers.ContainsKey($Matches[1])){return $Matches[1]};return ''}
    $id=if($verb -eq 'update'){$Arguments[3]}else{$Arguments[2]};Assert-Id $id
    if(-not $script:containers.ContainsKey($id)){Stop-Prod 'Synthetic mutation target missing.'}
    $current=$script:containers[$id];$script:events.Add("${verb}:$id")
    switch($verb){
      'update' {$parts=$Arguments[2].Substring(10).Split(':');$current.Record.RestartPolicy=[pscustomobject]@{Name=$parts[0];MaximumRetryCount=$(if($parts.Count -eq 2){[int]$parts[1]}else{0})}}
      'stop' {$current.Raw.State.Running=$false}
      'start' {$current.Raw.State.Running=$true}
      'rename' {if($script:failRename -eq $id){Stop-Prod 'Synthetic rename failure.'};$current.Record.Name=$Arguments[3]}
      'rm' {Assert-Test ((Get-Field $current.Raw.Config.Labels 'com.docker.compose.project') -ceq 'agent-production') 'never remove an original/unknown container';$script:containers.Remove($id)}
      default {Stop-Prod 'Unexpected synthetic mutation.'}
    }
    ''
  }
}

Test-Case 'import is inert and PowerShell 5.1 syntax parses' {Assert-Test ($PSVersionTable.PSVersion.Major -eq 5) 'Windows PowerShell 5.1 validation';Assert-Test ($parseErrors.Count -eq 0) 'syntax valid'}
Test-Case 'raw UTF-8 env round-trip preserves quotes dollars hashes equals spaces and empty values' {
  $entries=@('WEBUI_SECRET_KEY=synthetic-$secret#=value','QUOTES="a" ''b''','DOLLARS=$HOME ${not-expanded}','HASH=#literal # value',
    'EQUALS=a=b=c','SPACES=  keep both  ','EMPTY=','UNICODE=Ω café')
  With-Mocks @{'Protect-ProdPath'={param($Path)}} {
    $path=Join-Path $scratch 'raw.env';Write-RawEnv $path $entries
    $actual=[IO.File]::ReadAllLines($path,[Text.UTF8Encoding]::new($false,$true))
    Assert-Test (@(Compare-Object $actual $entries -CaseSensitive).Count -eq 0) 'exact raw entries'
    $bytes=[IO.File]::ReadAllBytes($path);Assert-Test (-not ($bytes[0] -eq 239 -and $bytes[1] -eq 187)) 'UTF8 without BOM'
    $original=Convert-EnvEntries $entries;$parsed=Convert-EnvEntries $actual
    foreach($key in $original.Keys){Assert-Test ($parsed[$key] -ceq $original[$key]) 'literal values'}
  }
  foreach($bad in @(@("A=a`nb"),@('A=a','A=b'),@('bad-key=value'),@('missing-equals'))){$null=Expect-Failure {Convert-EnvEntries $bad} 'environment'}
}
Test-Case 'ACL failure prevents secret writing' {
  With-Mocks @{'Protect-ProdPath'={param($Path) Stop-Prod 'Synthetic ACL failure.'}} {
    $path=Join-Path $scratch 'must-not-exist';$null=Expect-Failure {Write-PrivateText $path 'synthetic-private'} 'ACL failure'
    Assert-Test (-not (Test-Path -LiteralPath $path)) 'no secret written without ACL'
  }
}
Test-Case 'null Compose labels are fingerprinted without storing environment secrets in recovery records' {
  $raw=[pscustomobject]@{Id=('1'*64);Name='/open-webui';Image='sha256:'+('a'*64);Config=[pscustomobject]@{Labels=$null;Env=@('WEBUI_SECRET_KEY=SYNTHETIC_PRIVATE')};
    HostConfig=[pscustomobject]@{RestartPolicy=[pscustomobject]@{Name='always';MaximumRetryCount=0};PortBindings=$null};Mounts=@();NetworkSettings=[pscustomobject]@{Networks=[pscustomobject]@{}}}
  With-Mocks @{'Read-DockerJson'={param($Arguments) $raw}} {
    $record=Read-ProdContainer ('1'*64)
    Assert-Test ($record.Record.Id -ceq ('1'*64)) 'full ID retained'
    Assert-Test ((ConvertTo-Json $record.Record -Depth 20) -notmatch 'SYNTHETIC_PRIVATE') 'recovery record has no env values'
  }
}
Test-Case 'complete cutover cold-backup ordering and exact restart restoration' {
  $s=New-TestState
  With-Mocks $script:orchestration {
    $null=Invoke-ProdCutover $s
    Assert-Test ($s.Phase -ceq 'ReadyForOperatorAcceptance') 'manual acceptance remains required'
    $events=@($script:events);$backup=[Array]::IndexOf($events,'backup:cold/webui');$up=[Array]::IndexOf($events,'compose-up')
    foreach($original in $s.Originals.PSObject.Properties.Value){Assert-Test ([Array]::IndexOf($events,('stop:'+$original.Id)) -lt $backup) 'all six originals cold before backup'}
    Assert-Test ([Array]::IndexOf($events,'verify:cold-chroma.tar') -lt $up) 'both archives verified before new services'
    $null=Invoke-ProdRollback $s
    Assert-Restored $s
    Assert-Test ((Get-RestartArgument $script:containers[$s.Originals.'knowledge-service'.Id].Record.RestartPolicy) -ceq 'on-failure:7') 'MaximumRetryCount restored'
  }
}
Test-Case 'identity configuration name and restart-policy changes block mutation' {
  foreach($kind in @('Id','Fingerprint','Name','RestartPolicy')){
    $s=New-TestState;$id=$s.Originals.'open-webui'.Id
    switch($kind){'Id'{$script:containers[$id].Record.Id=('f'*64)};'Fingerprint'{$script:containers[$id].Record.Fingerprint='changed'};
      'Name'{$script:containers[$id].Record.Name='unknown'};'RestartPolicy'{$script:containers[$id].Record.RestartPolicy=[pscustomobject]@{Name='on-failure';MaximumRetryCount=9}}}
    With-Mocks $script:orchestration {$null=Expect-Failure {Stop-Original $s 'open-webui'} 'changed';Assert-Test ($script:events.Count -eq 0) 'no mutation after mismatch'}
  }
  foreach($policy in @([pscustomobject]@{Name='always';MaximumRetryCount=2},[pscustomobject]@{Name='on-failure';MaximumRetryCount=$true})){
    $null=Expect-Failure {Get-RestartArgument $policy} 'restart policy'
  }
}
Test-Case 'partial rename failure rolls back originals without blind retry or historical restarts' {
  $s=New-TestState;$script:failRename=$s.Originals.'knowledge-service'.Id
  With-Mocks $script:orchestration {
    $null=Expect-Failure {Invoke-ProdCutover $s} 'rename failure'
    Assert-Test ($s.Phase -ceq 'Renaming') 'phase records partial rename'
    $script:failRename='';$null=Invoke-ProdRollback $s;Assert-Restored $s
  }
}
Test-Case 'partial Compose start records exact created IDs and rollback removes only those replacements' {
  $s=New-TestState;$script:failCompose=$true
  With-Mocks $script:orchestration {
    $null=Expect-Failure {Invoke-ProdCutover $s} 'partial Compose'
    Assert-Test (@($s.Replacements.PSObject.Properties).Count -eq 1) 'partial replacement was recorded in finally'
    $null=Invoke-ProdRollback $s;Assert-Restored $s
    Assert-Test (@($s.RemovedIds).Count -eq 1) 'only one known replacement removed'
  }
}
Test-Case 'archive hash failure stops before rename startup and supports rollback without restore' {
  $s=New-TestState;$script:failArchive=$true
  With-Mocks $script:orchestration {
    $null=Expect-Failure {Invoke-ProdCutover $s} 'hash mismatch'
    Assert-Test (@($script:events | Where-Object {$_ -like 'rename:*' -or $_ -eq 'compose-up'}).Count -eq 0) 'no adoption on bad backup'
    $script:failArchive=$false;$null=Invoke-ProdRollback $s;Assert-Restored $s
  }
}
Test-Case 'rollback preserves failed-state data before guarded restore and compares file hashes' {
  $s=New-TestState
  With-Mocks $script:orchestration {
    $null=Invoke-ProdCutover $s;$null=Invoke-ProdRollback $s -RestoreData
    Assert-Restored $s;Assert-Test ($s.FailedState.Count -eq 1) 'failed-state pair retained'
    $firstRestore=[Array]::IndexOf(@($script:events),'restore-data')
    $lastFailedVerify=@($script:events | Select-Object -First $firstRestore | Where-Object {$_ -like 'verify:failed-*'}).Count
    Assert-Test ($lastFailedVerify -eq 2) 'both failed-state archives verified before overwriting any data'
  }
}
Test-Case 'partial data restore failure leaves originals quiescent and retains failed-state backups' {
  $s=New-TestState
  With-Mocks $script:orchestration {
    $null=Invoke-ProdCutover $s;$script:failRestore=$true
    $null=Expect-Failure {Invoke-ProdRollback $s -RestoreData} 'extraction failure'
    Assert-Test ($s.Phase -ceq 'RestoringData' -and $s.FailedState.Count -eq 1) 'recoverable data-restore checkpoint'
    foreach($entry in $s.Originals.PSObject.Properties){Assert-Test (-not $script:containers[$entry.Value.Id].Raw.State.Running) 'all originals still stopped'}
  }
}
Test-Case 'unknown replacement occupant is never stopped or removed' {
  $s=New-TestState
  With-Mocks $script:orchestration {
    $null=Invoke-ProdCutover $s;$id=$s.Replacements.'open-webui'
    $script:containers[$id].Raw.Config.Labels.'com.docker.compose.project'='unknown'
    $before=$script:events.Count;$null=Expect-Failure {Stop-ProdReplacements $s} 'Unknown owner'
    Assert-Test ($script:events.Count -eq $before) 'unknown occupant untouched'
  }
}
Test-Case 'safe archive listings include hidden files and refuse traversal links duplicates and special entries' {
  Assert-ArchiveListing @('./','./.hidden','./db') @('drwx','-rw','-rw')
  foreach($names in @(@('../escape'),@('/absolute'),@('C:/escape'),@('./x/../../escape'),@('./bad\path'),@('./x','./x'))){
    $null=Expect-Failure {Assert-ArchiveListing $names @($names | ForEach-Object {'-rw'})} 'Unsafe archive'
  }
  foreach($kind in @('lrwx','hrw','brw')){$null=Expect-Failure {Assert-ArchiveListing @('./link') @($kind)} 'Unsafe archive'}
}
Test-Case 'exact storage path guards reject computed paths outside the approved root' {
  $null=Expect-Failure {Assert-ChildPath 'E:\elsewhere\knowledge-db' 'E:\AI-Agent-Workspace'} 'escapes'
  $null=Expect-Failure {Assert-ChildPath 'E:\AI-Agent-Workspace\runtime\..\..\other' 'E:\AI-Agent-Workspace\runtime'} 'escapes'
  $s=New-TestState;$s.DatabasePath='E:\unrelated'
  With-Mocks $script:orchestration {$null=Expect-Failure {Get-DataMount $s chroma -Writable} 'Unapproved database path'}
}
Test-Case 'external databases fail before quiescence and do not echo URL credentials' {
  $web=Convert-EnvEntries @('WEBUI_SECRET_KEY=synthetic')
  $kv=Convert-EnvEntries @('CHROMA_HOST=chromadb','CHROMA_PORT=8000','LM_STUDIO_URL=http://host.docker.internal:1234/v1/embeddings',
    'EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5@q8_0','WATCHER_ENABLED=true','WATCH_INTERVAL_SECONDS=60')
  $chroma=Convert-EnvEntries @('PERSIST_DIRECTORY=/data')
  Assert-DatabaseConfiguration $web $kv $chroma
  $web.Add('DATABASE_URL','postgres://SYNTHETIC_PASSWORD@external/db')
  $message=Expect-Failure {Assert-DatabaseConfiguration $web $kv $chroma} 'Unsupported external'
  Assert-Test (-not $message.Contains('SYNTHETIC_PASSWORD')) 'secret-safe database diagnostic'
}
Test-Case 'captured port bindings preserve loopback IPv4 and IPv6 without broadening exposure' {
  $ui=[pscustomobject]@{Raw=[pscustomobject]@{HostConfig=[pscustomobject]@{PortBindings=[pscustomobject]@{'8080/tcp'=@(
    [pscustomobject]@{HostIp='127.0.0.1';HostPort='3000'},[pscustomobject]@{HostIp='::1';HostPort='3000'})}}}}
  $ports=@(Get-ProdPorts $ui);Assert-Test ($ports.Count -eq 2) 'all bindings retained'
  Assert-Test (@($ports | Where-Object {$_.host_ip -in @('127.0.0.1','::1')}).Count -eq 2) 'no all-host default inserted'
}
Test-Case 'observed null extra-host device and port metadata passes main mount validation' {
  $s=New-TestState;$script:startup=[pscustomobject]@{Cmd=@('run');Entrypoint=$null;WorkingDir='/app';User=''}
  $entries=@()
  foreach($name in @('open-webui','knowledge-service','chromadb')){
    $entry=$script:containers[$s.Originals.$name.Id]
    $entry.Raw | Add-Member -NotePropertyName Config -NotePropertyValue $script:startup -Force
    $entry.Raw.HostConfig=[pscustomobject]@{ExtraHosts=$null;Privileged=$false;Devices=$null;DeviceRequests=$null;PortBindings=$null}
    $entry.Raw.NetworkSettings.Networks=[pscustomobject]@{'agent-internal'=[pscustomobject]@{};bridge=[pscustomobject]@{}}
    $entries+=$entry
  }
  $entries[0].Record.Mounts=@([pscustomobject]@{Type='volume';Name='open-webui';Destination='/app/backend/data';RW=$true})
  $entries[1].Record.Mounts=@([pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\input';Destination='/workspace/input';RW=$true},
    [pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\knowledge';Destination='/workspace/knowledge';RW=$true})
  $entries[2].Record.Mounts=@([pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\knowledge-db';Destination='/data';RW=$true})
  With-Mocks @{'Read-DockerJson'={param($Arguments) [pscustomobject]@{Config=$script:startup}};'Assert-NoReparse'={param($Path,[switch]$Tree)}} {
    Assert-MainMounts $entries[0] $entries[1] $entries[2] 'E:\AI-Agent-Workspace'
    $entries[1].Raw.HostConfig.PortBindings=[pscustomobject]@{'8002/tcp'=@([pscustomobject]@{HostPort='8002'})}
    $null=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] 'E:\AI-Agent-Workspace'} 'published ports'
  }
}
Test-Case 'native errors never expose captured stdout stderr or synthetic secret values' {
  $native=(Get-Command Invoke-ProdNative).ScriptBlock
  With-Mocks @{'git'={Write-Output 'SYNTHETIC_SECRET stdout';Write-Error 'SYNTHETIC_SECRET stderr' -ErrorAction Continue;$global:LASTEXITCODE=1}} {
    $message=Expect-Failure {& $native Git @('mock-only') 'synthetic validation'} 'PROD-GUARD'
    Assert-Test (-not $message.Contains('SYNTHETIC_SECRET')) 'native output withheld'
  }
}

Test-Case 'atomic phase state retains original recovery and the previous checkpoint' {
  $s=New-TestState
  With-Mocks @{'Protect-ProdPath'={param($Path)}} {
    Save-ProdState $s;$s.Phase='Quiescing';Save-ProdState $s
    $prior=ConvertFrom-Json ([IO.File]::ReadAllText($s.StatePath+'.previous'))
    $current=ConvertFrom-Json ([IO.File]::ReadAllText($s.StatePath))
    $recovery=ConvertFrom-Json ([IO.File]::ReadAllText((Join-Path $scratch 'original-recovery.json')))
    Assert-Test ($prior.Phase -ceq 'Prepared' -and $current.Phase -ceq 'Quiescing') 'previous checkpoint retained atomically'
    Assert-Test ($recovery.Originals.'knowledge-service'.RestartPolicy.MaximumRetryCount -eq 7) 'immutable exact policy recovery'
  }
}
Test-Case 'actual archive-hash mismatch blocks helper access before extraction' {
  $s=New-TestState;$path=Join-Path $scratch 'cold-webui.tar';[IO.File]::WriteAllText($path,'synthetic-corrupt-archive')
  $archive=[pscustomobject]@{Path=$path;Hash=('0'*64)}
  With-Mocks @{'Invoke-Helper'={throw 'TEST: helper must not run after a hash mismatch'}} {
    $null=Expect-Failure {Assert-Archive $s $archive} 'Archive hash mismatch'
  }
}
Test-Case 'streamed archived file bytes must match the cold source hashes including dotfiles' {
  $s=New-TestState;$path=Join-Path $scratch 'cold-webui.tar';$manifest=Join-Path $scratch 'cold-webui.files'
  $payload=[Text.Encoding]::UTF8.GetBytes('synthetic bytes');$header=New-Object byte[] 512
  $name=[Text.Encoding]::ASCII.GetBytes('./.hidden');[Array]::Copy($name,0,$header,0,$name.Length)
  $size=[Text.Encoding]::ASCII.GetBytes([Convert]::ToString($payload.Length,8).PadLeft(11,'0')+[char]0)
  [Array]::Copy($size,0,$header,124,12);$header[156]=48
  $stream=[IO.MemoryStream]::new()
  try{$stream.Write($header,0,512);$stream.Write($payload,0,$payload.Length);$pad=New-Object byte[] (512-$payload.Length+1024);$stream.Write($pad,0,$pad.Length);[IO.File]::WriteAllBytes($path,$stream.ToArray())}finally{$stream.Dispose()}
  [IO.File]::WriteAllText($manifest,(Get-TextHash 'synthetic bytes')+"  ./.hidden`n",[Text.UTF8Encoding]::new($false))
  $archive=[pscustomobject]@{Path=$path;Manifest=$manifest;Names=@('./.hidden')}
  Assert-TarFileHashes $s $archive
  $bytes=[IO.File]::ReadAllBytes($path);$bytes[512]=0;[IO.File]::WriteAllBytes($path,$bytes)
  $null=Expect-Failure {Assert-TarFileHashes $s $archive} 'contents differ'
}
Test-Case 'Git export sends binary archive to output file and retains all tracked runtime files' {
  $s=New-TestState;$s.Release=Join-Path $scratch 'synthetic-release'
  $script:tracked=@('scripts/document_service_production.py','scripts/document_pipeline_v06c.py','scripts/knowledge_adapter_v04c.py',
    'tools/analyze_document_production.py','knowledge-service/server.py','scripts/task_planner_v04b.py')
  $script:exportCalls=[Collections.Generic.List[object]]::new()
  With-Mocks @{
    'Protect-ProdPath'={param($Path)}
    'Read-ArchiveMetadata'={param($State,$Path,[switch]$AllowEmpty) [pscustomobject]@{Names=$script:tracked}}
    'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
      $script:exportCalls.Add([pscustomobject]@{Kind=$Kind;Arguments=$Arguments;Input=$InputText})
      if($Kind -eq 'Git'){
        Assert-Test ($Arguments -contains 'archive') 'git archive used'
        $output=@($Arguments | Where-Object {$_.StartsWith('--output=')})
        Assert-Test ($output.Count -eq 1) 'explicit output path prevents binary PowerShell pipelines'
        [IO.File]::WriteAllText($output[0].Substring(9),'synthetic-tar')
      }else{
        Assert-Test ($Arguments -contains 'none' -and $Arguments -contains '--network') 'imports have no network'
        Assert-Test (@($Arguments | Where-Object {$_ -match 'secrets|token|env-file'}).Count -eq 0) 'imports have no credentials'
      }
      ''
    }
    'Invoke-Helper'={param($State,$Mounts,$Code)
      foreach($file in $script:tracked){$path=Join-Path $State.Release $file;$null=[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path));[IO.File]::WriteAllText($path,'synthetic source')}
    }
  } {
    Export-ProdRelease $s $script:tracked
    Assert-Test (@(Get-ChildItem $s.Release -Recurse -File).Count -eq $script:tracked.Count) 'all tracked dependencies exported'
    Assert-Test ($script:exportCalls.Count -eq 3) 'only mocked archive and two credential-free import checks'
  }
}
Test-Case 'expected-HEAD and tracked-cleanliness failures precede any container mutation' {
  foreach($failure in @('head','working','index')){
    $script:preflightFailure=$failure;$script:preflightCalls=[Collections.Generic.List[string]]::new()
    With-Mocks @{'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
      $script:preflightCalls.Add($Kind+':'+($Arguments -join ' '))
      if($Kind -eq 'Docker' -and $Arguments[0] -eq 'compose'){return '5.6.1'}
      if($Kind -eq 'Git' -and $Arguments -contains 'rev-parse'){if($script:preflightFailure -eq 'head'){return ('b'*40)};return ('a'*40)}
      if($Kind -eq 'Git' -and $Arguments -contains 'diff'){
        if(($script:preflightFailure -eq 'working' -and $Arguments -notcontains '--cached') -or
           ($script:preflightFailure -eq 'index' -and $Arguments -contains '--cached')){Stop-Prod 'Tracked cleanliness failed.'};return ''
      }
      throw 'TEST: preflight advanced past the intended failure'
    }} {
      $null=Expect-Failure {Invoke-ProdPrepare ('a'*40)} 'Expected commit|cleanliness'
      Assert-Test (@($script:preflightCalls | Where-Object {$_ -match 'container|stop|rename|run '}).Count -eq 0) 'no container access/mutation before source validation'
    }
  }
}
Test-Case 'running alias collisions and stale DNS answers block acceptance without stopping owners' {
  $s=New-TestState
  With-Mocks $script:orchestration {
    $null=Invoke-ProdCutover $s
    $counter=10
    foreach($name in @('open-webui','knowledge-service','document-planner-prod','chromadb')){
      $counter++;$id=if($name -eq 'chromadb'){$s.Originals.chromadb.Id}else{$s.Replacements.$name}
      $networks=[ordered]@{'agent-internal'=[pscustomobject]@{NetworkID=('d'*64);Aliases=@($name);IPAddress="198.18.0.$counter";GlobalIPv6Address=''}}
      if($name -ne 'chromadb'){$networks['agent-production-egress']=[pscustomobject]@{NetworkID=('e'*64);Aliases=@($name);IPAddress="198.19.0.$counter";GlobalIPv6Address=''}}
      $script:containers[$id].Raw.NetworkSettings.Networks=[pscustomobject]$networks
    }
    $script:staleDns=$false
    With-Mocks @{
      'Read-DockerJson'={param($Arguments)
        $network=$Arguments[-1];$members=[ordered]@{}
        foreach($entry in $script:containers.Values){if(Get-Field $entry.Raw.NetworkSettings.Networks $network){$members[$entry.Record.Id]=[pscustomobject]@{Name=$entry.Record.Name}}}
        [pscustomobject]$members
      }
      'Invoke-ContainerProbe'={param($Id,$Code)
        $answers=[ordered]@{}
        foreach($name in @('open-webui','knowledge-service','document-planner-prod','chromadb')){
          $entry=@($script:containers.Values | Where-Object {$_.Record.Name -ceq $name})[0]
          $answers[$name]=@($entry.Raw.NetworkSettings.Networks.'agent-internal'.IPAddress)
        }
        if($script:staleDns){$answers['knowledge-service']=@('203.0.113.1')};[pscustomobject]$answers
      }
    } {
      Assert-AliasOwners $s
      $script:staleDns=$true;$null=Expect-Failure {Assert-AliasOwners $s} 'DNS answers';$script:staleDns=$false
      $foreignId='f'*64
      $script:containers[$foreignId]=[pscustomobject]@{Record=[pscustomobject]@{Id=$foreignId;Name='foreign-owner'};Raw=[pscustomobject]@{
        State=[pscustomobject]@{Running=$true};NetworkSettings=[pscustomobject]@{Networks=[pscustomobject]@{'agent-internal'=[pscustomobject]@{Aliases=@('knowledge-service');IPAddress='203.0.113.2'}}}}}
      $before=$script:events.Count
      $message=Expect-Failure {Assert-AliasOwners $s} 'DNS alias collision'
      Assert-Test ($message.Contains($foreignId) -and $script:events.Count -eq $before) 'actionable owner ID, no automatic stop'
    }
  }
}
Test-Case 'wrong mounts reset data and a non-production API profile all fail readiness' {
  $s=New-TestState
  With-Mocks $script:orchestration {
    $null=Invoke-ProdCutover $s
    foreach($name in @('open-webui','knowledge-service','document-planner-prod')){
      $s.ExpectedMounts | Add-Member -NotePropertyName $name -NotePropertyValue @([pscustomobject]@{target='/expected';type='bind';source='E:\expected';read_only=$true})
    }
    $null=Expect-Failure {Assert-ReplacementConfiguration $s} 'mount count changed'
    # The orchestration harness replaces readiness. Use the real definition from
    # the parsed file for these targeted rejection cases, with IO still mocked.
    $ast=[Management.Automation.Language.Parser]::ParseFile($helper,[ref]$null,[ref]$null)
    $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-ProductionReady'},$true)
    $realReady=[scriptblock]::Create('param($State)'+[Environment]::NewLine+$definition.Body.Extent.Text.TrimStart('{').TrimEnd('}'))
    With-Mocks @{
      'Assert-ReplacementConfiguration'={param($State)};'Assert-AliasOwners'={param($State)}
      'Get-UiBaseline'={param($Id) [pscustomobject]@{users=0;chats=0}}
    } {$null=Expect-Failure {& $realReady $s} 'user/chat counts'}
    With-Mocks @{
      'Assert-ReplacementConfiguration'={param($State)};'Assert-AliasOwners'={param($State)}
      'Get-UiBaseline'={param($Id) [pscustomobject]@{users=2;chats=3}}
      'Get-KnowledgeBaseline'={param($Id,[switch]$Planner) [pscustomobject]@{chunks=0;files=0;documents=0;identities='reset'}}
    } {$null=Expect-Failure {& $realReady $s} 'catalog/index'}
    With-Mocks @{
      'Assert-ReplacementConfiguration'={param($State)};'Assert-AliasOwners'={param($State)}
      'Get-UiBaseline'={param($Id) [pscustomobject]@{users=2;chats=3}}
      'Get-KnowledgeBaseline'={param($Id,[switch]$Planner) [pscustomobject]@{chunks=5;files=2;documents=2;identities='baseline'}}
      'Invoke-ContainerProbe'={param($Id,$Code) [pscustomobject]@{profile='test';pipeline='0.6g';auth=$false}}
    } {$null=Expect-Failure {& $realReady $s} 'authentication/profile'}
  }
}

Write-Output "PowerShell $($PSVersionTable.PSVersion): $script:passed mocked deployment tests passed. No Docker/Git/container/deployment actions executed."

if($ValidateCompose){
  # Only this explicit branch may call the real CLI, solely for offline config.
  $compose=(Get-Command docker.exe -CommandType Application -ErrorAction Stop).Source
  $root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
  $template=Join-Path $scratch 'synthetic-compose.yml'
  $envFile=Join-Path $scratch 'empty.env';[IO.File]::WriteAllText($envFile,'',[Text.UTF8Encoding]::new($false))
  $synthetic=@('WEBUI_SECRET_KEY=synthetic-only','QUOTES="literal"','DOLLARS=$literal ${NOT_EXPANDED}','HASH=#literal','EQUALS=a=b=c',
    'SPACES=  keep both  ','EMPTY=','UNICODE=Ω café')
  [IO.File]::WriteAllText((Join-Path $scratch 'synthetic.env'),($synthetic -join "`n")+"`n",[Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText($template,[IO.File]::ReadAllText((Join-Path $root 'deployment/docker-compose.production.yml')).Replace('/webui.env','/synthetic.env').Replace('/knowledge.env','/synthetic.env'),[Text.UTF8Encoding]::new($false))
  $override=Join-Path $scratch 'synthetic-override.json'
  [IO.File]::WriteAllText($override,'{"services":{"open-webui":{"ports":[{"target":8080,"published":"3000","host_ip":"127.0.0.1"}]}}}',[Text.UTF8Encoding]::new($false))
  $keys=@('PROD_RELEASE_DIR','PROD_STATE_DIR','PROD_WORKSPACE_DIR');$old=@{}
  foreach($key in $keys){$old[$key]=[Environment]::GetEnvironmentVariable($key,'Process')}
  try{
    $env:PROD_RELEASE_DIR=$scratch;$env:PROD_STATE_DIR=$scratch;$env:PROD_WORKSPACE_DIR=$scratch
    $output=@(& $compose compose --project-name agent-production --env-file $envFile -f $template -f $override config --format json 2>&1)
    if($LASTEXITCODE -ne 0){throw 'Synthetic Compose config failed; native output withheld.'}
    $configuration=ConvertFrom-Json ($output -join "`n")
    $values=Convert-EnvEntries $synthetic
    # Compose escapes literal dollars as $$ when rendering a reusable config.
    # Validate that representation; never double dollars in the raw env files.
    foreach($key in $values.Keys){Assert-Test ($configuration.services.'open-webui'.environment.$key -ceq $values[$key].Replace('$','$$')) "Compose raw env preserves exact synthetic values ($key)"}
    Assert-Test (@($configuration.services.PSObject.Properties).Count -eq 3) 'only three services'
    Assert-Test (-not $configuration.services.'knowledge-service'.ports -and -not $configuration.services.'document-planner-prod'.ports) 'no API/knowledge published ports'
    Write-Output 'Synthetic Compose config/raw-env validation passed; no real environment was loaded or printed.'
  }finally{foreach($key in $keys){[Environment]::SetEnvironmentVariable($key,$old[$key],'Process')}}
}
