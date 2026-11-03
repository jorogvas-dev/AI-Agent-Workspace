# All orchestration tests mock Docker/Git. Optional validation runs only Compose
# config against synthetic env files, never production environments or containers.
[CmdletBinding()]param([switch]$ValidateCompose,
  [string]$PythonExecutable=(Join-Path (Split-Path $PSScriptRoot -Parent) '.venv/Scripts/python.exe'))
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

# Execute only embedded deployment probes, with fake HTTP/Chroma/runpy objects.
# No application modules, credential files, Docker, Git or network are accessed.
$pythonHarness=Join-Path $scratch 'synthetic-probes.py'
[IO.File]::WriteAllText($pythonHarness,@'
import base64,contextlib,hashlib,io,json,os,runpy,socket,subprocess,sys,types,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from unittest.mock import patch
case=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
os.environ.clear()
os.environ.update(KNOWLEDGE_TOKEN='synthetic-offline-token',CHROMA_HOST='chromadb',CHROMA_PORT='8000')
if case.get('mode')=='helper-bootstrap':
    calls=[]; stdout=io.StringIO(); stderr=io.StringIO(); exit_code=None
    def shell(args,**kwargs):
        assert args==['sh','-s'] and set(kwargs)=={'input'}
        assert type(kwargs['input']) is bytes
        calls.append(dict(args=args,input_base64=base64.b64encode(kwargs['input']).decode()))
        # Inherited streams must be preserved, with no bootstrap chatter.
        print('synthetic shell stdout'); print('synthetic shell stderr',file=sys.stderr)
        return types.SimpleNamespace(returncode=case['exit_code'])
    stdin=types.SimpleNamespace(buffer=io.BytesIO(base64.b64decode(case['input_base64'])))
    try:
        with patch.object(subprocess,'run',shell), patch.object(sys,'stdin',stdin), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try: exec(compile(case['code'],'<helper-bootstrap>','exec'),{})
            except SystemExit as stop: exit_code=stop.code
        assert len(calls)==1
        result=dict(ok=True,exit_code=exit_code,calls=calls,stdout=stdout.getvalue(),stderr=stderr.getvalue())
    except Exception as exc: result=dict(ok=False,error=type(exc).__name__)
    print(json.dumps(result)); sys.exit(0)
if case.get('mode')=='storage':
    import copy,stat,tarfile
    namespace={'__name__':'synthetic_storage'}
    exec(compile(case['code'],'<production-storage>','exec'),namespace)
    fault=case.get('fault'); Unsafe=namespace['UnsafeStorage']
    nodes={'.':dict(kind='d'),'.cache':dict(kind='d'),'.cache/blobs':dict(kind='d'),
        '.cache/blobs/hash':dict(kind='f',data=b'cached bytes'),'.cache/snapshots':dict(kind='d'),
        '.cache/snapshots/rev':dict(kind='d'),'.cache/snapshots/rev/config.json':dict(kind='l',target='../../blobs/hash'),
        'webui.db':dict(kind='f',data=b'synthetic database'),'.hidden':dict(kind='f',data=b'hidden bytes')}
    link='.cache/snapshots/rev/config.json'
    if fault=='escape': nodes[link]['target']='../../../outside'
    if fault=='absolute': nodes[link]['target']='/etc/passwd'
    if fault=='dangling': nodes[link]['target']='../../blobs/missing'
    if fault=='cycle': nodes[link]['target']='config.json'
    if fault=='indirect-cycle':
        nodes[link]['target']='other'; nodes['.cache/snapshots/rev/other']=dict(kind='l',target='config.json')
    if fault=='parent-escape':
        nodes[link]['target']='../../alias/../../../outside'; nodes['.cache/alias']=dict(kind='l',target='blobs')
    if fault=='hard': nodes['hard']=dict(kind='h',target='webui.db',data=b'synthetic database')
    if fault in ('special','block'): nodes['fifo']=dict(kind='s',block=fault=='block')
    if fault in ('under-link','under-link-first'):
        nodes['.cache/alias']=dict(kind='l',target='blobs'); nodes['.cache/alias/child']=dict(kind='f',data=b'unsafe')
    if fault=='chain':
        nodes[link]['target']='../../alias/hash'; nodes['.cache/alias']=dict(kind='l',target='blobs')
    if fault in ('long','gnu-long'):
        name='long-'+('x'*110); nodes['.cache/blobs/'+name]=dict(kind='f',data=b'long target'); nodes[link]['target']='../../blobs/'+name
    if fault=='metadata':
        nodes['A']=dict(kind='f',data=b'uppercase'); nodes['a']=dict(kind='f',data=b'lowercase')
    if fault=='linked-db':
        nodes['database-real']=nodes['webui.db']; nodes['webui.db']=dict(kind='l',target='database-real')
    if fault=='db-empty': nodes['webui.db']['data']=b''
    if fault=='db-missing': del nodes['webui.db']
    if fault=='db-directory': nodes['webui.db']=dict(kind='d')
    class VirtualStorage:
        O_RDONLY=0; O_NOFOLLOW=1; O_DIRECTORY=2
        def __init__(self,records): self.records=records; self.fds={}; self.next=0
        def key(self,name,fd): return name if self.fds[fd][0]=='.' else self.fds[fd][0]+'/'+name
        def info(self,key):
            r=self.records[key]; kinds={'d':stat.S_IFDIR,'f':stat.S_IFREG,'h':stat.S_IFREG,'l':stat.S_IFLNK,'s':stat.S_IFIFO}
            return types.SimpleNamespace(st_mode=(stat.S_IFBLK if r.get('block') else kinds[r['kind']])|0o750,st_dev=1,st_ino=sorted(self.records).index(key)+1,
                st_size=len(r.get('data',r.get('target','').encode())),st_mtime_ns=123,st_nlink=2 if r['kind']=='h' else 1)
        def open(self,name,flags,dir_fd=None):
            assert flags & self.O_NOFOLLOW
            if dir_fd is None: assert name=='/source'; key='.'
            else: key=self.key(name,dir_fd)
            assert self.records[key]['kind'] in ('d','f','h'), 'never dereference a symbolic link'
            if flags & self.O_DIRECTORY: assert self.records[key]['kind']=='d'
            self.next+=1; self.fds[self.next]=[key,0]; return self.next
        def close(self,fd): del self.fds[fd]
        def fstat(self,fd): return self.info(self.fds[fd][0])
        def stat(self,name,*,dir_fd,follow_symlinks): assert follow_symlinks is False; return self.info(self.key(name,dir_fd))
        def readlink(self,name,*,dir_fd): return self.records[self.key(name,dir_fd)]['target']
        def listdir(self,fd):
            root=self.fds[fd][0]; prefix='' if root=='.' else root+'/'
            return [key[len(prefix):] for key in self.records if key!='.' and key.startswith(prefix) and '/' not in key[len(prefix):]]
        def read(self,fd,size):
            key,offset=self.fds[fd]; data=self.records[key]['data'][offset:offset+size]; self.fds[fd][1]+=len(data); return data
    def scanned(records):
        namespace['os']=VirtualStorage(records)
        try: return namespace['scan']('/source',required='webui.db')
        finally: namespace['os']=os
    tar_path=Path(sys.argv[1]).with_suffix('.tar')
    def make_tar(records,format=tarfile.USTAR_FORMAT):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w',format=format,pax_headers={'comment':'synthetic commit'} if fault=='global-comment' else None) as archive:
            for key,r in sorted(records.items(),reverse=fault=='under-link-first'):
                item=tarfile.TarInfo('.' if key=='.' else './'+key); item.uid=1000; item.gid=1001; item.mode=0o750
                if fault=='unknown-pax': item.pax_headers={'SCHILY.xattr.synthetic':'unsupported'}
                if fault=='global-path': item.pax_headers={'size':'1'}
                if fault=='pax-time': item.mtime=123.125
                if fault=='pax-escape' and key=='.hidden': item.pax_headers={'path':'../escape'}
                if fault=='pax-under-link' and key=='.hidden': item.pax_headers={'path':'./.cache/snapshots/rev/config.json/child'}
                if fault=='pax-link-escape' and key==link: item.pax_headers={'linkpath':'/absolute'}
                if r['kind']=='d': item.type=tarfile.DIRTYPE
                elif r['kind']=='l': item.type=tarfile.SYMTYPE; item.linkname=r['target']
                elif r['kind']=='h': item.type=tarfile.LNKTYPE; item.linkname=r['target']
                elif r['kind']=='s': item.type=tarfile.BLKTYPE if r.get('block') else tarfile.FIFOTYPE
                else: item.size=len(r['data'])
                archive.addfile(item,io.BytesIO(r['data']) if r['kind']=='f' else None)
            if fault in ('duplicate','traversal','absolute-member','backslash-member'):
                name={'duplicate':'./webui.db','traversal':'../escape','absolute-member':'/absolute','backslash-member':'./bad\\path'}[fault]
                archive.addfile(tarfile.TarInfo(name),io.BytesIO(b''))
        data=output.getvalue()
        if fault=='trailer': data+=b'X'*512
        if fault=='truncated': data=data[:513]
        if fault=='checksum': data=bytes([data[0]^1])+data[1:]
        tar_path.write_bytes(data)
    try:
        scan_error=None; archive_error=None
        try: source=scanned(nodes)
        except Unsafe as exc: scan_error=str(exc)
        format=tarfile.GNU_FORMAT if fault=='gnu-long' else tarfile.PAX_FORMAT if fault in ('long','unknown-pax','global-path','global-comment','pax-time','pax-escape','pax-under-link','pax-link-escape') else tarfile.USTAR_FORMAT
        make_tar(nodes,format)
        try: result=namespace['archive'](str(tar_path))
        except (Unsafe,tarfile.TarError,ValueError) as exc: archive_error=type(exc).__name__
        if fault in ('escape','absolute','dangling','cycle','indirect-cycle','parent-escape','hard','special','block','under-link','under-link-first','unknown-pax','global-path','trailer','truncated','checksum','duplicate','traversal','absolute-member','backslash-member','pax-escape','pax-under-link','pax-link-escape','db-empty','db-missing','db-directory'):
            print(json.dumps(dict(ok=True,scan_rejected=scan_error is not None,archive_rejected=archive_error is not None))); sys.exit(0)
        assert scan_error is None and archive_error is None
        preflight=VirtualStorage(nodes)
        preflight.read=lambda *args: (_ for _ in ()).throw(AssertionError('preflight must not read regular bytes'))
        namespace['os']=preflight
        try: namespace['scan']('/source',hash_files=False,required='webui.db')
        finally: namespace['os']=os
        expected=namespace['manifests'](source)
        assert expected==namespace['manifests'](result['entries'])
        # Reconstruct only synthetic regular bytes and exact link metadata from
        # the validated archive; no host symbolic links or real tar commands.
        restored={}
        with tarfile.open(tar_path,mode='r:') as archive:
            for item in archive:
                key=namespace['member_name'](item.name)
                assert (item.uid,item.gid,item.mode)==(1000,1001,0o750)
                if item.isdir(): restored[key]=dict(kind='d')
                elif item.issym(): restored[key]=dict(kind='l',target=item.linkname)
                else: restored[key]=dict(kind='f',data=archive.extractfile(item).read())
        assert restored[link]['target']==nodes[link]['target']
        def restored_manifests():
            captured=[]; namespace['os']=VirtualStorage(restored)
            namespace['write_manifests']=lambda prefix,entries: captured.append((prefix,namespace['manifests'](entries)))
            try: namespace['run'](dict(action='scan',prefix='restored'))
            finally: namespace['os']=os
            assert len(captured)==1 and captured[0][0]=='/out/restored'
            return captured[0][1]
        assert restored_manifests()==expected
        original_archive=namespace['archive']
        namespace['archive']=lambda path,allow_links=True: original_archive(str(tar_path),allow_links)
        namespace['bounded_read']=lambda path: expected[0 if path.endswith('.files') else 1]
        namespace['run'](dict(action='verify',archive='cold-webui.tar',prefix='cold-webui'))
        if fault=='alter-target':
            nodes[link]['target']='./../../blobs/hash'; make_tar(nodes)
            changed=original_archive(str(tar_path))
            assert namespace['manifests'](changed['entries'])[0]==expected[0]
            try: namespace['run'](dict(action='verify',archive='cold-webui.tar',prefix='cold-webui'))
            except Unsafe as exc: assert 'link targets' in str(exc)
            else: raise AssertionError('altered archive target accepted')
            restored[link]['target']='./../../blobs/hash'
            assert restored_manifests()[1]!=expected[1] and restored_manifests()[0]==expected[0]
        if fault=='file-hash':
            nodes['.hidden']['data']=b'altered file'; make_tar(nodes)
            try: namespace['run'](dict(action='verify',archive='cold-webui.tar',prefix='cold-webui'))
            except Unsafe as exc: assert 'file contents' in str(exc)
            else: raise AssertionError('altered file bytes accepted')
            restored['.hidden']['data']=b'altered file'
            assert restored_manifests()[0]!=expected[0]
        if fault=='metadata':
            metadata_path=Path(sys.argv[1]).with_suffix('.metadata')
            namespace['open']=lambda path,*args,**kwargs: open(metadata_path if path=='/out/synthetic.metadata' else path,*args,**kwargs)
            namespace['run'](dict(action='archive',archive='cold-webui.tar',result='synthetic.metadata'))
            metadata=json.loads(metadata_path.read_bytes())
            assert set(metadata)=={'names','files','links','inventory_hash'}
            print(json.dumps(dict(ok=True,archive_path=str(tar_path),metadata=metadata,
                files_base64=base64.b64encode(expected[0]).decode(),links_base64=base64.b64encode(expected[1]).decode()))); sys.exit(0)
        print(json.dumps(dict(ok=True,files=result['files'],links=result['links'],target=restored[link]['target']))); sys.exit(0)
    except SystemExit: raise
    except Exception as exc: print(json.dumps(dict(ok=False,error=type(exc).__name__,detail=str(exc)))); sys.exit(0)
calls=[]; output=io.StringIO(); result=None; ok=False; error=None
fault=case.get('fault'); size=case.get('size',53); mode=case.get('mode','catalog')
docs=[]
for n in range(size):
    root='input' if n%2 else 'knowledge'; source='folder/file-'+str(n)+'.txt'
    docs.append(dict(folder=root,source=source,document_id=hashlib.sha256((root+':'+source).encode()).hexdigest()))
if fault=='root': docs[0]['folder']='other'
if fault=='source': docs[0]['source']='../escape.txt'
if fault=='source-backslash': docs[0]['source']='folder\\file.txt'
if fault=='source-control': docs[0]['source']='file\x00.txt'.encode().decode('unicode_escape')
if fault=='source-long': docs[0]['source']='x'*513
if fault=='id': docs[0]['document_id']='f'*64
if fault=='duplicate': docs[-1]=docs[0].copy()
class Response(io.BytesIO):
    status=200
class Opener:
    def open(self,request,timeout):
        assert timeout==15
        url=request.full_url; calls.append('http:'+url)
        base='http://knowledge-service:8002' if case.get('planner') else 'http://127.0.0.1:8002'
        assert url.startswith(base+'/')
        if url==base+'/health':
            data=dict(status='ok',chunks=200,indexed_files=size)
        else:
            assert request.get_header('Authorization')=='Bearer synthetic-offline-token'
            offset=int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['offset'][0])
            if mode in ('legacy','auth','forbidden','server-error') or (mode=='later-404' and offset):
                code={'legacy':404,'auth':401,'forbidden':403,'server-error':500,'later-404':404}[mode]
                raise urllib.error.HTTPError(url,code,'synthetic',{},io.BytesIO(b''))
            if mode=='timeout': raise TimeoutError('synthetic')
            if mode=='malformed': return Response(b'{')
            if mode=='oversized': return Response(b'x'*65537)
            if mode=='redirect': raise RuntimeError('redirect refused')
            rows=docs[offset:offset+50]; end=offset+len(rows); total=size
            if fault=='total-drift' and offset: total+=1
            p=dict(limit=50,offset=offset,total=total,has_more=end<total,next_offset=end if end<total else None)
            if fault=='offset': p['offset']=offset+1
            if fault=='boolean': p['has_more']=1
            if fault=='next-offset': p['next_offset']=offset
            if fault=='result-count': rows=rows[:-1]
            data=dict(status='success',documents=rows,result_count=len(rows),pagination=p)
        return Response(json.dumps(data).encode())
def build_opener(handler):
    try: handler.redirect_request(None,None,None,None,None,None)
    except RuntimeError: pass
    else: raise AssertionError('redirects must be rejected')
    return Opener()
class Collection:
    counts=0
    def count(self):
        calls.append('chroma:count'); self.counts+=1
        return size+1 if fault=='count-drift' and self.counts>1 else size
    def get(self,*,limit,offset,include):
        calls.append('chroma:get:'+str(offset))
        assert limit==50 and include==['metadatas']
        rows=docs[offset:offset+limit]
        data=dict(ids=[x['document_id'] for x in rows],metadatas=[dict(root=x['folder'],source=x['source']) for x in rows])
        if fault=='short-page': data['ids']=data['ids'][:-1]; data['metadatas']=data['metadatas'][:-1]
        if fault=='metadata-length': data['metadatas']=[]
        if fault=='metadata-oversized': data['metadatas'][0]['extra']='x'*65537
        return data
    def __getattr__(self,name):
        calls.append('forbidden:'+name); raise AssertionError('only count/get allowed')
class Client:
    def get_collection(self,*,name):
        calls.append('chroma:get_collection:'+name); assert name=='agent_file_state'
        return Collection()
    def __getattr__(self,name):
        calls.append('forbidden:'+name); raise AssertionError('only get_collection allowed')
def client(*,host,port,settings):
    calls.append('chroma:client'); assert host=='chromadb' and port==8000 and settings=={'anonymized_telemetry':False}
    return Client()
fake=types.ModuleType('chromadb'); fake.HttpClient=client
config=types.ModuleType('chromadb.config'); config.Settings=lambda **kw: kw
def synthetic_token(path,*args,**kwargs):
    assert path.as_posix()=='/run/secrets/agent_knowledge_token'
    calls.append('synthetic-token'); return 'synthetic-offline-token'
def import_check(path,*,run_name):
    assert path=='/release/knowledge-service/server.py' and run_name=='release_import_validation'
    # Model the real server's import-time requirement without importing the app.
    assert os.environ['KNOWLEDGE_TOKEN']=='synthetic-import-validation-only'
    calls.append('synthetic-import-with-token')
def forbidden_network(*args,**kwargs): raise AssertionError('network forbidden')
try:
    with patch.dict(sys.modules,{'chromadb':fake,'chromadb.config':config}), patch('urllib.request.build_opener',build_opener), patch.object(Path,'read_text',synthetic_token), patch.object(runpy,'run_path',import_check), patch.object(socket,'socket',forbidden_network), contextlib.redirect_stdout(output):
        exec(compile(case['code'],'<deployment-probe>','exec'),{})
    if output.getvalue(): result=json.loads(output.getvalue())
    ok=True
except Exception as exc: error=type(exc).__name__
print(json.dumps(dict(ok=ok,error=error,result=result,calls=calls)))
'@,[Text.UTF8Encoding]::new($false))
function Invoke-SyntheticProbe([string]$Code,[string]$Mode='catalog',[string]$Fault='',[int]$Size=53,[switch]$Planner,
  [byte[]]$InputBytes=@(),[int]$ExitCode=0) {
  if(-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)){throw 'TEST: supply -PythonExecutable for the offline standard-library probe harness.'}
  $fixture=Join-Path $scratch ('probe-'+[guid]::NewGuid().ToString('N')+'.json')
  [IO.File]::WriteAllText($fixture,(ConvertTo-Json @{code=$Code;mode=$Mode;fault=$Fault;size=$Size;planner=[bool]$Planner;
    input_base64=[Convert]::ToBase64String($InputBytes);exit_code=$ExitCode}),[Text.UTF8Encoding]::new($false))
  $raw=@(& $PythonExecutable -I -B $pythonHarness $fixture 2>&1)
  if($LASTEXITCODE -ne 0){throw 'TEST: synthetic probe harness failed; native output withheld.'}
  ConvertFrom-Json ($raw -join "`n")
}
function Get-SyntheticBaselineCode([switch]$Planner) {
  With-Mocks @{'Invoke-ContainerProbe'={param($Id,$Code) $Code}} { Get-KnowledgeBaseline ('1'*64) -Planner:$Planner }
}
function Get-SyntheticHelperInvocation([string]$Code,[string[]]$Mounts=@()) {
  With-Mocks @{
    'Read-DockerJson'={param($Arguments)
      Assert-Test (($Arguments -join ' ') -ceq ('image inspect --format {{json .Id}} '+(Get-ProdImages).helper)) 'helper image is verified by its unchanged cached ID'
      (Get-ProdImages).helper
    }
    'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
      [pscustomobject]@{Kind=$Kind;Arguments=$Arguments;InputText=$InputText}
    }
  } { Invoke-Helper $null $Mounts $Code }
}

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
function New-TestMainServices {
  $s=New-TestState;$entries=@()
  $script:startup=[pscustomobject]@{Cmd=@('run');Entrypoint=$null;WorkingDir='/app';User=''}
  $script:startupJson=@{container=@{};image=@{}}
  $script:startupCalls=[Collections.Generic.List[object]]::new();$script:wholeStartupReads=0;$script:startupFailure=$null
  foreach($name in @('open-webui','knowledge-service','chromadb')){
    $entry=$script:containers[$s.Originals.$name.Id]
    $entry.Raw.Config=$script:startup
    $entry.Raw.HostConfig=[pscustomobject]@{ExtraHosts=$null;Privileged=$false;Devices=$null;DeviceRequests=$null;PortBindings=$null}
    $entry.Raw.NetworkSettings.Networks=[pscustomobject]@{'agent-internal'=[pscustomobject]@{};bridge=[pscustomobject]@{}}
    if($name -ne 'chromadb'){
      $entry.Record.Image=(Get-ProdImages)[$(if($name -eq 'open-webui'){'webui'}else{'knowledge'})]
      $s.Originals.$name.Image=$entry.Record.Image
      foreach($kind in @('container','image')){
        $reference=if($kind -eq 'container'){$entry.Record.Id}else{$entry.Record.Image}
        $script:startupJson[$kind][$reference]=@{Cmd='["run"]';Entrypoint='null';WorkingDir='"/app"';User='""'}
      }
    }
    $entries+=$entry
  }
  $entries[0].Record.Mounts=@([pscustomobject]@{Type='volume';Name='open-webui';Destination='/app/backend/data';RW=$true})+@(
    foreach($name in @('output','scripts','temp')){[pscustomobject]@{Type='bind';Source="E:\AI-Agent-Workspace\$name";Destination="/workspace/$name";RW=$true}})
  $entries[1].Record.Mounts=@([pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\input';Destination='/workspace/input';RW=$true},
    [pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\knowledge';Destination='/workspace/knowledge';RW=$true})
  $entries[2].Record.Mounts=@([pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\knowledge-db';Destination='/data';RW=$true})
  [pscustomobject]@{State=$s;Entries=$entries}
}
$script:startupMocks=@{
  'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
    $script:startupCalls.Add([pscustomobject]@{Kind=$Kind;Arguments=$Arguments})
    Assert-Test ($Kind -ceq 'Docker' -and $Arguments.Count -eq 5 -and $Arguments[0] -cin @('container','image') -and
      $Arguments[1] -ceq 'inspect' -and $Arguments[2] -ceq '--format') 'startup performs selected-field inspection only'
    $match=[regex]::Match($Arguments[3],'^\{\{json \.Config\.(Cmd|Entrypoint|WorkingDir|User)\}\}$')
    Assert-Test $match.Success 'exact selected startup template'
    $setting=$match.Groups[1].Value;$reference=$Arguments[4]
    Assert-Test ($script:startupJson[$Arguments[0]].ContainsKey($reference)) 'verified container ID or pinned image ID used'
    if($script:startupFailure -ceq ($reference+':'+$setting)){throw 'SYNTHETIC_PRIVATE_INSPECTION_FAILURE'}
    $script:startupJson[$Arguments[0]][$reference][$setting]
  }
  'Read-DockerJson'={param($Arguments)
    $script:wholeStartupReads++
    # Faithful legacy image inspect: omits the empty User default entirely.
    [pscustomobject]@{Config=[pscustomobject]@{Cmd=@('run');Entrypoint=$null;WorkingDir='/app'}}
  }
  'Assert-NoReparse'={param($Path,[switch]$Tree)}
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
    [pscustomobject]@{Path=(Join-Path $State.Directory "$Prefix-$Target.tar");Hash='hash';NamesHash='names';Files=1;Links=0;Names=@('./db');Manifest=(Join-Path $State.Directory "$Prefix-$Target.files");ManifestHash='manifest';
      LinkManifest=(Join-Path $State.Directory "$Prefix-$Target.links");LinkManifestHash='links'}
  }
  'Assert-Archive'={param($State,$Archive) $script:events.Add('verify:'+([IO.Path]::GetFileName($Archive.Path)));if($script:failArchive){Stop-Prod 'Archive hash mismatch.'}}
  'Invoke-Helper'={param($State,$Mounts,$Code)
    if($Code.Contains('find . -mindepth')){
      Assert-Test ($State.FailedState.Count -gt 0) 'failed-state backup precedes extraction'
      Assert-Test ($Mounts[0] -in @('type=volume,source=open-webui,target=/source','type=bind,source=E:\AI-Agent-Workspace\knowledge-db,target=/source')) 'restore mount stays exact'
      Assert-Test ($Code.Contains('cmp /out/restored.files /archive/')) 'restore file hashes checked'
      Assert-Test ($Code.Contains('cmp /out/restored.links /archive/')) 'restore exact link targets/directories checked separately'
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

function New-TestAcceptanceState {
  $state=New-TestState
  With-Mocks $script:orchestration {$null=Invoke-ProdCutover $state}
  # Keep synthetic release paths below Windows PowerShell 5.1's MAX_PATH ceiling.
  $state.Root=Join-Path ([IO.Path]::GetTempPath()) ('pa-'+[guid]::NewGuid().ToString('N').Substring(0,8))
  $state.Directory=Join-Path (Join-Path $state.Root 'runtime/production/private') $state.RunId
  $state.StatePath=Join-Path $state.Directory 'state.json';$state.DatabasePath=Join-Path $state.Root 'knowledge-db'
  $state.Release=Join-Path (Join-Path (Join-Path $state.Root 'runtime/production/releases') $state.Commit) $state.RunId
  $state.EgressId='e'*64
  $null=[IO.Directory]::CreateDirectory($state.Directory);$null=[IO.Directory]::CreateDirectory($state.Release)
  [IO.File]::WriteAllText((Join-Path $state.Release 'synthetic-runtime.txt'),'synthetic release only',[Text.UTF8Encoding]::new($false))
  $state.ReleaseHash=Get-ReleaseHash $state.Release
  $state | Add-Member -NotePropertyName Override -NotePropertyValue (Join-Path $state.Directory 'compose.json')
  $state | Add-Member -NotePropertyName Template -NotePropertyValue (Join-Path $state.Directory 'compose.yml')
  $services=[ordered]@{}
  foreach($name in @('open-webui','knowledge-service','document-planner-prod')){
    $services[$name]=[ordered]@{labels=@{'io.agent-production.run'=$state.RunId;'io.agent-production.commit'=$state.Commit}}
    $state.ExpectedMounts | Add-Member -NotePropertyName $name -NotePropertyValue @()
  }
  $services['open-webui'].restart='no';$services['open-webui'].ports=@(@{target=8080;published='3000';host_ip='127.0.0.1'})
  [IO.File]::WriteAllText($state.Override,(ConvertTo-Json @{services=$services;volumes=@{'open-webui'=@{external=$true;name='open-webui'}}} -Depth 30),[Text.UTF8Encoding]::new($false))
  $hashes=[ordered]@{}
  foreach($file in @('webui.env','knowledge.env','agent_knowledge_token','document_planner_prod_token','compose.json','compose.yml','empty.env')){
    $path=Join-Path $state.Directory $file
    if($file -cne 'compose.json'){[IO.File]::WriteAllText($path,'synthetic-only',[Text.UTF8Encoding]::new($false))}
    $hashes[$file]=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
  }
  $state | Add-Member -NotePropertyName PrivateHashes -NotePropertyValue ([pscustomobject]$hashes)
  $ui=$script:containers[$state.Replacements.'open-webui']
  $ui.Raw.HostConfig.PortBindings=[pscustomobject]@{'8080/tcp'=@([pscustomobject]@{HostIp='127.0.0.1';HostPort='3000'})}
  $state | Add-Member -NotePropertyName UiPorts -NotePropertyValue @(Get-ProdPorts $ui)
  $state | Add-Member -NotePropertyName UiEnvHash -NotePropertyValue (Get-EnvHash @())
  $state | Add-Member -NotePropertyName KnowledgeEnvHash -NotePropertyValue (Get-EnvHash @())
  $script:acceptanceState=$state;$script:acceptanceFailure='';$script:acceptanceFailureAfterUpdate=$false;$script:acceptanceIgnoreUpdate=$false;$script:acceptanceReadinessFailure=$false
  With-Mocks @{'Protect-ProdPath'={param($Path)}} {Save-ProdState $state}
  $script:events.Clear();$state
}
$script:acceptanceMocks=@{}
foreach($key in $script:orchestration.Keys){$script:acceptanceMocks[$key]=$script:orchestration[$key]}
$script:acceptanceMocks.Remove('Assert-Release');$script:acceptanceMocks.Remove('Save-ProdState')
$script:acceptanceMocks['Protect-ProdPath']={param($Path)}
$script:acceptanceMocks['Get-ProdRoot']={$script:acceptanceState.Root}
$script:acceptanceMocks['Assert-Storage']={param($State) $script:events.Add('storage-verified')}
$script:acceptanceMocks['Assert-ProductionNetworks']={param($State,[switch]$Create,[switch]$ReadOnly)
  Assert-Test ($ReadOnly -and -not $Create) 'acceptance only inspects recorded networks';$script:events.Add('networks-verified')
}
$script:acceptanceMocks['Assert-ProductionReady']={param($State)
  Assert-BackupsDormant $State;Assert-ReplacementConfiguration $State
  $script:events.Add('ready');if($script:acceptanceReadinessFailure){Stop-Prod 'Synthetic readiness failure.'}
}
$script:acceptanceMocks['Invoke-ProdCompose']={throw 'TEST: acceptance must never run Compose or recreate containers'}
$script:acceptanceMocks['Invoke-ProdNative']={param($Kind,$Arguments,$Operation,$InputText)
  $acceptUpdate=$script:acceptanceState.Phase -ceq 'Accepting' -and $Kind -ceq 'Docker' -and $Arguments[1] -ceq 'update' -and $Arguments[2] -ceq '--restart=unless-stopped'
  if($acceptUpdate){
    Assert-Test ($Arguments.Count -eq 4 -and $Arguments[3] -cin @($script:acceptanceState.Replacements.PSObject.Properties.Value)) 'updates target precisely recorded replacement IDs'
    Assert-Test ($script:events -contains 'ready') 'readiness precedes policy mutations'
    if($script:acceptanceFailure -ceq $Arguments[3] -and -not $script:acceptanceFailureAfterUpdate){Stop-Prod 'Synthetic interrupted policy update.'}
    if($script:acceptanceIgnoreUpdate){return ''}
  }
  $result=& ($script:orchestration['Invoke-ProdNative']) $Kind $Arguments $Operation $InputText
  if($acceptUpdate -and $script:acceptanceFailure -ceq $Arguments[3] -and $script:acceptanceFailureAfterUpdate){Stop-Prod 'Synthetic interrupted policy update.'}
  $result
}

Test-Case 'import is inert and PowerShell 5.1 syntax parses' {Assert-Test ($PSVersionTable.PSVersion.Major -eq 5) 'Windows PowerShell 5.1 validation';Assert-Test ($parseErrors.Count -eq 0) 'syntax valid'}
Test-Case 'central helper Python bootstrap preserves every sandbox argument mount and cached-image guard' {
  $mounts=@('type=volume,source=open-webui,target=/source,readonly',"type=bind,source=$scratch,target=/out")
  $code="for tool in tar find; do`r`n  command -v `"`$tool`" >/dev/null`r`ndone"
  $run=Get-SyntheticHelperInvocation $code $mounts;$bootstrap=$run.Arguments[-1]
  $expected=@('run','--rm','-i','--pull','never','--network','none','--read-only','--user','0:0','--cap-drop','ALL',
    '--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER','--cap-add','FSETID','--security-opt','no-new-privileges:true',
    '--pids-limit','64','--tmpfs','/tmp:rw,size=16m','--entrypoint','python')
  foreach($mount in $mounts){$expected+=@('--mount',$mount)}
  $expected+=@((Get-ProdImages).helper,'-B','-c',$bootstrap)
  Assert-Test ($run.Kind -ceq 'Docker' -and (Get-ObjectHash $run.Arguments) -ceq (Get-ObjectHash $expected)) 'only entrypoint/command changes; all sandbox flags and narrow mounts preserved'
  Assert-Test ($run.InputText -ceq ("set -eu`n"+$code)) 'set -eu prefix and original shell text enter the central byte bootstrap'
  $script:helperNativeCalled=$false
  With-Mocks @{
    'Read-DockerJson'={param($Arguments) 'sha256:'+('0'*64)}
    'Invoke-ProdNative'={$script:helperNativeCalled=$true;throw 'TEST: must reject uncached image before running helper'}
  } {
    $null=Expect-Failure {Invoke-Helper $null @() $code} 'Cached backup helper is unavailable'
    Assert-Test (-not $script:helperNativeCalled) 'no alternate image or pull on failed cache identity'
  }
}
Test-Case 'bootstrap normalizes all byte newline forms preserves UTF-8 and shell constructs and propagates exit/streams' {
  $unicode=([string][char]0x03A9)+' caf'+[char]0x00E9
  $lines=@('set -eu',('for item in ''{0}'' ''literal $HOME''; do' -f $unicode),
    '  if test -n "$item"; then','    printf ''%s\n'' "$item"','  fi','done')
  $expected=$lines -join "`n";$code=($lines | Select-Object -Skip 1) -join "`n"
  $run=Get-SyntheticHelperInvocation $code
  Assert-Test ($run.Arguments[-2] -ceq '-c' -and $run.InputText -ceq $expected) 'execute actual centrally supplied Python source and set -eu payload'
  $mixed=[Text.StringBuilder]::new();$endings=@("`r`n","`r","`n")
  for($i=0;$i -lt $lines.Count;$i++){
    $null=$mixed.Append($lines[$i]);if($i -lt $lines.Count-1){$null=$mixed.Append($endings[$i%3])}
  }
  $variants=@(@{Name='LF';Text=$expected;Expected=$expected},
    @{Name='CRLF';Text=$expected.Replace("`n","`r`n");Expected=$expected},
    @{Name='lone CR';Text=$expected.Replace("`n","`r");Expected=$expected},
    @{Name='mixed';Text=$mixed.ToString();Expected=$expected},
    @{Name='terminal CRLF';Text=$expected+"`r`n";Expected=$expected+"`n"})
  foreach($variant in $variants){foreach($exitCode in @(0,2,7,125)){
    $bytes=[Text.Encoding]::UTF8.GetBytes($variant.Text)
    $check=Invoke-SyntheticProbe $run.Arguments[-1] -Mode helper-bootstrap -InputBytes $bytes -ExitCode $exitCode
    Assert-Test ($check.ok -and $check.exit_code -eq $exitCode) "$($variant.Name) propagates shell exit $exitCode"
    Assert-Test (($check.calls[0].args -join '|') -ceq 'sh|-s') 'shell receives an argument list, without a host shell'
    Assert-Test ($check.calls[0].input_base64 -ceq [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($variant.Expected))) "$($variant.Name) preserves intended multiline UTF-8 bytes after normalization"
    Assert-Test ($check.stdout -ceq "synthetic shell stdout`n" -and $check.stderr -ceq "synthetic shell stderr`n") 'streams inherited exactly; bootstrap adds no output'
  }}
}
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
Test-Case 'cache-style relative links survive archive and synthetic restore without dereferencing' {
  foreach($fault in @('valid','chain','long','gnu-long','global-comment','pax-time','linked-db')){
    $check=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault $fault
    Assert-Test $check.ok "validated storage/archive/restored inventory for $fault ($($check.detail))"
    Assert-Test ($check.files -ge 3 -and $check.links -ge 1) 'all files, hidden entries and exact link metadata preserved'
  }
}
Test-Case 'escaping absolute dangling cyclic hard and special storage entries fail closed' {
  foreach($fault in @('escape','absolute','dangling','cycle','indirect-cycle','parent-escape','hard','special','block')){
    $check=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault $fault
    Assert-Test ($check.ok -and $check.scan_rejected -and $check.archive_rejected) "$fault rejected in both source scan and archive"
  }
}
Test-Case 'required SQLite database must resolve to a present nonempty regular file' {
  foreach($fault in @('db-empty','db-missing','db-directory')){
    $check=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault $fault
    Assert-Test ($check.ok -and $check.scan_rejected) "$fault cannot pass cold-storage preflight"
  }
}
Test-Case 'archive members beneath links traversal duplicates and unsupported formats are rejected before extraction' {
  foreach($fault in @('under-link','under-link-first','duplicate','traversal','absolute-member','backslash-member','unknown-pax','global-path','pax-escape','pax-under-link','pax-link-escape','trailer','truncated','checksum')){
    $check=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault $fault
    Assert-Test ($check.ok -and $check.archive_rejected) "unsafe or incompletely validated archive $fault rejected"
  }
}
Test-Case 'altered link spelling is detected independently of regular bytes before and after restore' {
  $check=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault alter-target
  Assert-Test $check.ok "exact targets checked independently ($($check.detail))"
}
Test-Case 'cold backup wiring binds separate link manifests and validates them before any restore' {
  $script:storageFixture=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault metadata
  Assert-Test $script:storageFixture.ok "synthetic metadata emitted by actual validator ($($script:storageFixture.detail))"
  $s=New-TestState;$script:storageVerifyCalls=0;$script:storageQuiescentCalls=0
  With-Mocks @{
    'Assert-Quiescent'={param($State) $script:storageQuiescentCalls++}
    'Protect-ProdPath'={param($Path)}
    'Get-DataMount'={param($State,$Target,[switch]$Writable) 'type=volume,source=open-webui,target=/source,readonly'}
    'Invoke-Helper'={param($State,$Mounts,$Code)
      Assert-Test ($script:storageQuiescentCalls -gt 0 -and $Mounts[0].EndsWith(',readonly')) 'quiescence and read-only source precede backup'
      $match=[regex]::Match($Code,"python -B - '([A-Za-z0-9+/=]+)' <<'PROD_STORAGE_PY'")
      Assert-Test $match.Success 'backup scans the whole storage tree using the shared validator'
      $parameters=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($match.Groups[1].Value)))
      Assert-Test ($parameters.action -ceq 'scan' -and $parameters.prefix -ceq 'cold-webui' -and $parameters.required -ceq 'webui.db') 'source file/link inventories captured before tar'
      Assert-Test ($Code.IndexOf('PROD_STORAGE_PY') -lt $Code.IndexOf('tar -cpf /out/cold-webui.tar .') -and
        $Code -notmatch 'tar (-h|--dereference)|--exclude') 'whole archive includes caches and never dereferences/excludes links'
      [IO.File]::WriteAllBytes((Join-Path $State.Directory 'cold-webui.tar'),[IO.File]::ReadAllBytes($script:storageFixture.archive_path))
      [IO.File]::WriteAllBytes((Join-Path $State.Directory 'cold-webui.files'),[Convert]::FromBase64String($script:storageFixture.files_base64))
      [IO.File]::WriteAllBytes((Join-Path $State.Directory 'cold-webui.links'),[Convert]::FromBase64String($script:storageFixture.links_base64))
    }
    'Invoke-StorageOperation'={param($State,$Mounts,$Action,$Parameters)
      Assert-Test ($Mounts[0] -ceq "type=bind,source=$($State.Directory),target=/archive,readonly") 'archive verification remains read-only and narrowly mounted'
      if($Action -ceq 'archive'){
        [IO.File]::WriteAllText((Join-Path $State.Directory $Parameters.result),(ConvertTo-Json $script:storageFixture.metadata -Depth 20 -Compress),[Text.UTF8Encoding]::new($false))
      }elseif($Action -ceq 'verify'){
        $script:storageVerifyCalls++
        Assert-Test ($Mounts.Count -eq 1 -and $Parameters.archive -ceq 'cold-webui.tar' -and $Parameters.prefix -ceq 'cold-webui') 'file/link manifests verified against the exact cold archive'
      }else{throw 'TEST: unexpected storage operation'}
    }
  } {
    $archive=New-ColdBackup $s webui cold
    Assert-Test ($archive.Links -eq 1 -and $archive.Files -eq 5 -and $archive.Names -ccontains './A' -and $archive.Names -ccontains './a') 'case-sensitive POSIX entries survive PowerShell metadata handling'
    Assert-Test ($archive.LinkManifestHash -ceq (Get-FileHash -LiteralPath $archive.LinkManifest -Algorithm SHA256).Hash) 'separate link inventory hash bound into recovery record'
    Assert-Archive $s $archive
    $verified=$script:storageVerifyCalls
    [IO.File]::AppendAllText($archive.LinkManifest,'synthetic alteration')
    $null=Expect-Failure {Assert-Archive $s $archive} 'link inventory hash mismatch'
    Assert-Test ($script:storageVerifyCalls -eq $verified) 'tampered link manifest aborts before restore/source access'
  }
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
Test-Case 'actual production mounts pass while unknown changed duplicate and wrong-mode mounts fail' {
  $fixture=New-TestMainServices;$s=$fixture.State;$entries=$fixture.Entries
  With-Mocks $script:startupMocks {
    Assert-MainMounts $entries[0] $entries[1] $entries[2] 'E:\AI-Agent-Workspace'
    $original=$entries[0].Record.Mounts
    foreach($bad in @([pscustomobject]@{Type='bind';Source='E:\AI-Agent-Workspace\other';Destination='/other';RW=$true},$original[1])){
      $entries[0].Record.Mounts=$original+@($bad)
      $null=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] $s.Root} 'Uncovered|Duplicate'
    }
    $entries[0].Record.Mounts=$original
    foreach($index in @(1,2,3)){
      $source=$original[$index].Source;$original[$index].Source='E:\AI-Agent-Workspace\changed'
      $null=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] $s.Root} 'bind source/type/mode'
      $original[$index].Source=$source;$original[$index].RW=$false
      $null=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] $s.Root} 'bind source/type/mode'
      $original[$index].RW=$true
    }
    $tracked=@('scripts/document_service_production.py','output/.gitkeep','temp/.gitkeep')
    foreach($mount in $original | Select-Object -Skip 1){
      $mapped=Convert-ProdMount $mount $s $tracked
      if($mount.Destination -ceq '/workspace/scripts'){
        Assert-Test ($mapped.source -ieq (Join-Path $s.Release 'scripts') -and $mapped.read_only) 'scripts use immutable release read-only'
      }else{Assert-Test ($mapped.source -ceq $mount.Source -and -not $mapped.read_only) 'output/temp remain exact writable binds even with tracked placeholders'}
    }
    $entries[1].Raw.HostConfig.PortBindings=[pscustomobject]@{'8002/tcp'=@([pscustomobject]@{HostPort='8002'})}
    $null=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] 'E:\AI-Agent-Workspace'} 'published ports'
  }
}
Test-Case 'selected startup inspection accepts matching empty defaults omitted by whole-image metadata' {
  $fixture=New-TestMainServices;$entries=$fixture.Entries
  With-Mocks $script:startupMocks {
    $whole=Read-DockerJson @('image','inspect','--format','{{json .}}',$entries[0].Record.Image)
    Assert-Test ($null -eq $whole.Config.PSObject.Properties['User'] -and $entries[0].Raw.Config.User -ceq '') 'whole-image fixture omits the empty default'
    $script:wholeStartupReads=0
    Assert-MainMounts $entries[0] $entries[1] $entries[2] $fixture.State.Root
    Assert-Test ($script:wholeStartupReads -eq 0 -and $script:startupCalls.Count -eq 16) 'all four settings inspected by full container and pinned image IDs for both services'
  }
}
Test-Case 'startup null empty single and multiple argument arrays retain their JSON structure' {
  foreach($json in @('null','[]','["only"]','["first","","last"]')){
    $fixture=New-TestMainServices;$entries=$fixture.Entries
    foreach($kind in @('container','image')){foreach($fields in $script:startupJson[$kind].Values){$fields.Cmd=$json;$fields.Entrypoint=$json}}
    # Insignificant JSON spacing may differ; argument order/empty strings may not.
    $script:startupJson.image[$entries[0].Record.Image].Cmd=$json.Replace(',',', ')
    With-Mocks $script:startupMocks {Assert-MainMounts $entries[0] $entries[1] $entries[2] $fixture.State.Root}
  }
}
Test-Case 'actual startup differences and null array string or argument-order distinctions abort safely' {
  $differences=@(
    @{Setting='Cmd';Container='["run"]';Image='["SYNTHETIC_PRIVATE_ARG"]'},
    @{Setting='Entrypoint';Container='["start"]';Image='["other"]'},
    @{Setting='WorkingDir';Container='"/app"';Image='"/other"'},
    @{Setting='User';Container='""';Image='"1000"'},
    @{Setting='User';Container='"runner"';Image='"Runner"'},
    @{Setting='Cmd';Container='null';Image='[]'},
    @{Setting='Entrypoint';Container='null';Image='[]'},
    @{Setting='Cmd';Container='[]';Image='[""]'},
    @{Setting='Cmd';Container='["run"]';Image='["run",""]'},
    @{Setting='Cmd';Container='["a","b"]';Image='["b","a"]'},
    @{Setting='Entrypoint';Container='["a","b"]';Image='["b","a"]'},
    @{Setting='WorkingDir';Container='null';Image='""'},
    @{Setting='User';Container='null';Image='""'})
  foreach($index in @(0,1)){foreach($difference in $differences){
    $fixture=New-TestMainServices;$entries=$fixture.Entries;$entry=$entries[$index];$setting=$difference.Setting
    $script:startupJson.container[$entry.Record.Id][$setting]=$difference.Container
    $script:startupJson.image[$entry.Record.Image][$setting]=$difference.Image
    With-Mocks $script:startupMocks {
      $message=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] $fixture.State.Root} ("Startup mismatch for $($entry.Record.Name) setting $setting")
      Assert-Test (-not $message.Contains('SYNTHETIC_PRIVATE_ARG')) 'diagnostic identifies container/setting without values'
    }
  }}
}
Test-Case 'startup inspection rejects invalid JSON and invalid setting types without leaking output' {
  $invalid=@(
    @{Setting='Cmd';Json=''},@{Setting='Cmd';Json='{'},@{Setting='Cmd';Json='null extra'},
    @{Setting='Cmd';Json='[],"setting":[]'},@{Setting='Cmd';Json='["one"] ["two"]'},
    @{Setting='Cmd';Json='"scalar"'},@{Setting='Cmd';Json='[null]'},@{Setting='Cmd';Json='[1]'},
    @{Setting='Entrypoint';Json='[["nested"]]'},@{Setting='Entrypoint';Json='{}'},
    @{Setting='WorkingDir';Json='[]'},@{Setting='User';Json='false'})
  foreach($kind in @('container','image')){foreach($bad in $invalid){
    $fixture=New-TestMainServices;$entries=$fixture.Entries;$entry=$entries[0]
    $reference=if($kind -eq 'container'){$entry.Record.Id}else{$entry.Record.Image}
    $script:startupJson[$kind][$reference][$bad.Setting]=$bad.Json
    With-Mocks $script:startupMocks {
      $null=Expect-Failure {Assert-MainMounts $entries[0] $entries[1] $entries[2] $fixture.State.Root} ("Startup inspection failed for open-webui setting $($bad.Setting)")
    }
  }}
}
Test-Case 'Prepare aborts on selected inspection failures or invalid JSON before any mutation' {
  foreach($kind in @('container','image')){foreach($failure in @('inspection','json')){
    $fixture=New-TestMainServices;$s=$fixture.State;$entry=$fixture.Entries[1];$script:unexpectedMutation=0
    $reference=if($kind -eq 'container'){$entry.Record.Id}else{$entry.Record.Image}
    if($failure -eq 'inspection'){$script:startupFailure=$reference+':User'}else{$script:startupJson[$kind][$reference].User='{SYNTHETIC_PRIVATE_JSON'}
    With-Mocks $script:startupMocks {
      With-Mocks @{
        'Find-ProdContainer'=$script:orchestration['Find-ProdContainer'];'Read-ProdContainer'=$script:orchestration['Read-ProdContainer']
        'Read-DockerJson'={param($Arguments)
          if($Arguments[0] -eq 'network'){return [pscustomobject]@{Name='agent-internal';Id=('f'*64)}}
          if($Arguments[0] -eq 'image' -and $Arguments[3] -ceq '{{json .Id}}'){return $Arguments[4]}
          throw 'TEST: unexpected whole-object startup inspection'
        }
        'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
          if($Kind -eq 'Git'){
            if($Arguments -contains 'rev-parse'){return ('a'*40)}
            if($Arguments -contains 'diff'){return ''}
            if($Arguments -contains 'ls-tree'){return 'scripts/document_service_production.py'}
          }
          if($Kind -eq 'Docker'){
            if($Arguments[0] -eq 'compose'){return '5.6.1'}
            if(($Arguments[0] -eq 'container' -and $Arguments[1] -eq 'ls') -or $Arguments[0] -eq 'network'){return ''}
            if($Arguments[1] -eq 'inspect'){return & ($script:startupMocks['Invoke-ProdNative']) $Kind $Arguments $Operation $InputText}
          }
          $script:unexpectedMutation++;throw 'TEST: unexpected native mutation'
        }
        'Assert-DatabaseConfiguration'={throw 'TEST: must abort before credential validation'}
        'New-PrivateDirectory'={$script:unexpectedMutation++;throw 'TEST: must not create private files'}
        'Save-ProdState'={$script:unexpectedMutation++;throw 'TEST: must not write state'}
      } {
        $message=Expect-Failure {Invoke-ProdPrepare ('a'*40)} 'Startup inspection failed for knowledge-service setting User'
        Assert-Test ($script:unexpectedMutation -eq 0 -and -not $message.Contains('SYNTHETIC_PRIVATE')) 'failure precedes mutations and withholds native/JSON details'
      }
    }
  }}
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
  $check=Invoke-SyntheticProbe (Get-StorageProgram) -Mode storage -Fault file-hash
  Assert-Test $check.ok "archive and restored regular-file bytes verified ($($check.detail))"
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
    $payload=$script:exportCalls[2].Input
    $check=Invoke-SyntheticProbe $payload
    Assert-Test ($check.ok -and $check.calls -contains 'synthetic-import-with-token') 'knowledge import sets its own synthetic required environment'
    $withoutToken=$payload.Replace("os.environ['KNOWLEDGE_TOKEN']='synthetic-import-validation-only'",'')
    $check=Invoke-SyntheticProbe $withoutToken
    Assert-Test (-not $check.ok) 'import harness rejects missing synthetic environment setup'
  }
}
Test-Case 'catalog and original-only legacy 404 produce the same bounded identity digest' {
  $code=Get-SyntheticBaselineCode
  $catalog=Invoke-SyntheticProbe $code
  $legacy=Invoke-SyntheticProbe $code -Mode legacy
  Assert-Test ($catalog.ok -and $legacy.ok) 'both original baseline paths validate actual probe code'
  Assert-Test ((Get-ObjectHash $catalog.result) -ceq (Get-ObjectHash $legacy.result)) 'catalog and metadata identities match'
  Assert-Test ($legacy.result.documents -eq 53 -and $legacy.result.chunks -eq 200 -and $legacy.result.files -eq 53) 'nonempty file/chunk counts retained'
  $ids=@(0..52 | ForEach-Object {$folder=if($_%2){'input'}else{'knowledge'};Get-TextHash ($folder+':folder/file-'+$_+'.txt')})
  $digest=Get-TextHash ((@($ids | Sort-Object -CaseSensitive)) -join "`n")
  Assert-Test ($legacy.result.identities -ceq $digest) 'digest contains validated source identities only'
  Assert-Test (@($legacy.result.PSObject.Properties).Count -eq 4) 'only counts and identity digest returned'
  Assert-Test (@($catalog.calls | Where-Object {$_ -like 'chroma:*'}).Count -eq 0) 'catalog-present never touches Chroma'
  Assert-Test ($legacy.calls -contains 'chroma:get_collection:agent_file_state' -and $legacy.calls -contains 'chroma:get:50') 'existing collection paginated read-only'
  Assert-Test (@($legacy.calls | Where-Object {$_ -like 'forbidden:*'}).Count -eq 0) 'no create/index/migrate/write methods used'
  $planner=Invoke-SyntheticProbe (Get-SyntheticBaselineCode -Planner) -Planner
  Assert-Test ($planner.ok -and $planner.calls -contains 'synthetic-token') 'replacement uses authenticated catalog'
  $refused=Invoke-SyntheticProbe (Get-SyntheticBaselineCode -Planner) -Mode legacy -Planner
  Assert-Test (-not $refused.ok -and @($refused.calls | Where-Object {$_ -like 'chroma:*'}).Count -eq 0) 'replacement 404 never falls back'
}
Test-Case 'authentication transport malformed oversized and later-page errors never enable legacy fallback' {
  $code=Get-SyntheticBaselineCode
  foreach($mode in @('auth','forbidden','server-error','timeout','malformed','oversized','redirect','later-404')){
    $check=Invoke-SyntheticProbe $code -Mode $mode
    Assert-Test (-not $check.ok) "baseline aborts on $mode"
    Assert-Test (@($check.calls | Where-Object {$_ -like 'chroma:*'}).Count -eq 0) "no fallback on $mode"
  }
}
Test-Case 'catalog and Chroma reject invalid identities empty inventories and count or pagination inconsistencies' {
  $code=Get-SyntheticBaselineCode
  foreach($mode in @('catalog','legacy')){
    foreach($fault in @('root','source','source-backslash','source-control','source-long','id','duplicate')){
      $check=Invoke-SyntheticProbe $code -Mode $mode -Fault $fault
      Assert-Test (-not $check.ok) "$mode rejects $fault"
    }
    foreach($size in @(0,10001)){
      $check=Invoke-SyntheticProbe $code -Mode $mode -Size $size
      Assert-Test (-not $check.ok) "$mode rejects empty/oversized inventory"
    }
  }
  foreach($fault in @('total-drift','offset','boolean','next-offset','result-count')){
    $check=Invoke-SyntheticProbe $code -Fault $fault
    Assert-Test (-not $check.ok -and @($check.calls | Where-Object {$_ -like 'chroma:*'}).Count -eq 0) "malformed catalog $fault aborts without fallback"
  }
  foreach($fault in @('count-drift','short-page','metadata-length','metadata-oversized')){
    $check=Invoke-SyntheticProbe $code -Mode legacy -Fault $fault
    Assert-Test (-not $check.ok) "legacy rejects $fault"
  }
}
Test-Case 'every missing backup or restore command aborts before quiescing production' {
  foreach($missing in @('tar','find','sha256sum','sort','cmp','rm')){
    $s=New-TestState;$script:missingCommand=$missing
    With-Mocks $script:orchestration {
      With-Mocks @{'Invoke-Helper'={param($State,$Mounts,$Code)
        Assert-Test ($Mounts.Count -eq 0) 'capability check has no data mounts'
        $match=[regex]::Match($Code,'for tool in ([a-z0-9 ]+); do command -v "\$tool" >/dev/null \|\| exit 1; done')
        Assert-Test $match.Success 'probe checks each command and fails on the first missing command'
        $commands=$match.Groups[1].Value.Split(' ')
        if($commands -contains $script:missingCommand){throw 'synthetic missing helper command'}
      }} {
        $null=Expect-Failure {Invoke-ProdCutover $s} 'helper must provide.*cmp and rm'
        Assert-Test ($s.Phase -ceq 'Prepared' -and @($script:events | Where-Object {$_ -match 'stop:|update:|Quiescing|backup:'}).Count -eq 0) 'all writers untouched on missing command'
        $null=Expect-Failure {Invoke-ProdRollback $s -RestoreData} 'helper must provide'
        Assert-Test ($s.Phase -ceq 'Prepared') 'data rollback also checks capabilities before stopping'
      }
    }
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

Test-Case 'acceptance persists only three restart policies and is idempotent after live/config readback' {
  $s=New-TestAcceptanceState;$original=ConvertFrom-Json ([IO.File]::ReadAllText($s.Override));$oldHashes=ConvertFrom-Json (ConvertTo-Json $s.PrivateHashes)
  With-Mocks $script:acceptanceMocks {
    $result=Invoke-ProdAccept $s
    Assert-Test ($result.Phase -ceq 'Accepted' -and $s.AcceptedAtUtc) 'accepted recorded only after readbacks'
    $after=ConvertFrom-Json ([IO.File]::ReadAllText($s.Override))
    foreach($name in @('open-webui','knowledge-service','document-planner-prod')){
      Assert-Test ($after.services.$name.restart -ceq 'unless-stopped') 'persisted Compose override controls future restarts'
      Assert-Test ((Get-RestartArgument $script:containers[$s.Replacements.$name].Record.RestartPolicy) -ceq 'unless-stopped') 'live policy exactly unless-stopped with zero retry count'
      if($original.services.$name.PSObject.Properties['restart']){$after.services.$name.restart=$original.services.$name.restart}else{$after.services.$name.PSObject.Properties.Remove('restart')}
    }
    Assert-Test ((Get-ObjectHash $after) -ceq (Get-ObjectHash $original)) 'all other Compose settings preserved'
    foreach($file in $oldHashes.PSObject.Properties.Name){
      if($file -ceq 'compose.json'){Assert-Test ($s.PrivateHashes.$file -ceq (Get-FileHash -LiteralPath $s.Override -Algorithm SHA256).Hash) 'integrity hash follows persisted policy configuration'}
      else{Assert-Test ($s.PrivateHashes.$file -ceq $oldHashes.$file) 'other private hashes unchanged'}
    }
    Assert-BackupsDormant $s
    $updates=@($script:events | Where-Object {$_ -like 'update:*'})
    Assert-Test ($updates.Count -eq 3 -and @($updates | Where-Object {$_ -notin @($s.Replacements.PSObject.Properties.Value | ForEach-Object {'update:'+$_})}).Count -eq 0) 'exactly three recorded replacements updated; no originals touched'
    $count=$updates.Count;$stateHash=(Get-FileHash -LiteralPath $s.StatePath -Algorithm SHA256).Hash;$time=$s.AcceptedAtUtc
    $null=Invoke-ProdAccept $s
    Assert-Test (@($script:events | Where-Object {$_ -like 'update:*'}).Count -eq $count -and $s.AcceptedAtUtc -ceq $time -and
      (Get-FileHash -LiteralPath $s.StatePath -Algorithm SHA256).Hash -ceq $stateHash) 'repeated acceptance is read-only and preserves acceptance time'
    Assert-Test (@($script:events | Where-Object {$_ -match 'start:|stop:|rm:|rename:|compose-up'}).Count -eq 0) 'acceptance never recreates, starts or stops containers'
  }
}
Test-Case 'Accept dispatch reads the existing schema-1 ReadyForOperatorAcceptance state without migration or Git' {
  $s=New-TestAcceptanceState
  With-Mocks $script:acceptanceMocks {
    $legacy=Read-ProdState $s.StatePath
    Assert-Test ($legacy.Schema -eq 1 -and -not $legacy.PSObject.Properties['Acceptance']) 'existing deployed state shape remains supported'
    $result=Invoke-ProdDeployment -Action Accept -StatePath $s.StatePath
    $loaded=Read-ProdState $s.StatePath
    Assert-Test ($result.Phase -ceq 'Accepted' -and $loaded.Phase -ceq 'Accepted' -and $loaded.Commit -ceq $s.Commit -and $loaded.ReleaseHash -ceq $s.ReleaseHash) 'same pinned release accepted with no new deployment'
  }
}
Test-Case 'partial policy failures before or after Docker mutation resume safely using recorded identities' {
  foreach($afterUpdate in @($false,$true)){
    $s=New-TestAcceptanceState;$script:acceptanceFailure=$s.Replacements.'knowledge-service';$script:acceptanceFailureAfterUpdate=$afterUpdate
    With-Mocks $script:acceptanceMocks {
      $null=Expect-Failure {Invoke-ProdAccept $s} 'interrupted policy update'
      $loaded=Read-ProdState $s.StatePath
      Assert-Test ($loaded.Phase -ceq 'Accepting') 'interruption never records Accepted'
      $script:acceptanceFailure='';$script:acceptanceState=$loaded
      $planner=$script:containers[$loaded.Replacements.'document-planner-prod'];$fingerprint=$planner.Record.Fingerprint
      $count=@($script:events | Where-Object {$_ -like 'update:*'}).Count;$planner.Record.Fingerprint='changed during interruption'
      $null=Expect-Failure {Invoke-ProdAccept $loaded} 'replacement configuration changed'
      Assert-Test (@($script:events | Where-Object {$_ -like 'update:*'}).Count -eq $count) 'resume rechecks every identity before updating any remaining policy'
      $planner.Record.Fingerprint=$fingerprint
      $null=Invoke-ProdAccept $loaded
      Assert-Test ($loaded.Phase -ceq 'Accepted') 'mixed no/unless-stopped policies resume after revalidation'
      Assert-Test (@($script:events | Where-Object {$_ -like 'update:*'}).Count -eq 3) 'already updated IDs are never updated twice'
    }
  }
}
Test-Case 'acceptance resumes across write-ahead config commit and integrity-hash checkpoint interruptions' {
  $realSave=(Get-Command Save-ProdState).ScriptBlock;$realArtifact=(Get-Command Write-AcceptanceArtifact).ScriptBlock
  foreach($point in @('before-config','after-config')){
    $s=New-TestAcceptanceState;$script:acceptanceCrashPoint=$point
    With-Mocks $script:acceptanceMocks {
      With-Mocks @{
        'Write-AcceptanceArtifact'={param($Path,$Text)
          if($script:acceptanceCrashPoint -ceq 'before-config' -and $Path.EndsWith('compose.accepting.new')){Stop-Prod 'Synthetic config interruption.'}
          & $realArtifact $Path $Text
        }
        'Save-ProdState'={param($State)
          if($script:acceptanceCrashPoint -ceq 'after-config' -and $State.Phase -ceq 'Accepting' -and $State.PrivateHashes.'compose.json' -ceq $State.Acceptance.ToHash){Stop-Prod 'Synthetic config interruption.'}
          & $realSave $State
        }
      } {$null=Expect-Failure {Invoke-ProdAccept $s} 'config interruption'}
      $loaded=Read-ProdState $s.StatePath
      Assert-Test ($loaded.Phase -ceq 'Accepting' -and $loaded.PrivateHashes.'compose.json' -ceq $loaded.Acceptance.FromHash) 'durable write-ahead state retains the original acknowledged hash'
      $actual=(Get-FileHash -LiteralPath $s.Override -Algorithm SHA256).Hash
      Assert-Test ($actual -ceq $(if($point -ceq 'before-config'){$loaded.Acceptance.FromHash}else{$loaded.Acceptance.ToHash})) 'actual config may be precisely the recorded old or new version'
      $script:acceptanceState=$loaded;$null=Invoke-ProdAccept $loaded
      Assert-Test ($loaded.Phase -ceq 'Accepted' -and $loaded.PrivateHashes.'compose.json' -ceq $loaded.Acceptance.ToHash) 'resume reconciles only the journaled config and integrity hash'
    }
  }
}
Test-Case 'identity configuration readiness and backup mismatches abort acceptance before mutations' {
  foreach($fault in @('identity','fingerprint','project','inventory','readiness','backup','unexpected-policy')){
    $s=New-TestAcceptanceState;$id=$s.Replacements.'open-webui';$oldHash=(Get-FileHash -LiteralPath $s.Override -Algorithm SHA256).Hash
    switch($fault){
      'identity'{$s.Replacements.'open-webui'='f'*64}
      'fingerprint'{$script:containers[$id].Record.Fingerprint='changed'}
      'project'{$script:containers[$id].Raw.Config.Labels.'com.docker.compose.project'='unknown'}
      'inventory'{$s.ReplacementFingerprints.PSObject.Properties.Remove('knowledge-service')}
      'readiness'{$script:acceptanceReadinessFailure=$true}
      'backup'{$script:containers[$s.Originals.'open-webui'.Id].Raw.State.Running=$true}
      'unexpected-policy'{$script:containers[$id].Record.RestartPolicy=[pscustomobject]@{Name='always';MaximumRetryCount=0}}
    }
    With-Mocks $script:acceptanceMocks {
      $null=Expect-Failure {Invoke-ProdAccept $s} 'identity|configuration|Unknown owner|inventory|readiness|must remain stopped|policy mismatch'
      Assert-Test ($s.Phase -ceq 'ReadyForOperatorAcceptance' -and -not (Test-Path -LiteralPath (Join-Path $s.Directory 'compose.accepted.json')) -and
        (Get-FileHash -LiteralPath $s.Override -Algorithm SHA256).Hash -ceq $oldHash -and
        @($script:events | Where-Object {$_ -like 'update:*'}).Count -eq 0) 'failed gates leave config, phase and live policies unchanged'
    }
  }
}
Test-Case 'failed live readback and changed pending configuration never record acceptance' {
  $s=New-TestAcceptanceState;$script:acceptanceIgnoreUpdate=$true
  With-Mocks $script:acceptanceMocks {
    $null=Expect-Failure {Invoke-ProdAccept $s} 'policy readback failed'
    Assert-Test ((Read-ProdState $s.StatePath).Phase -ceq 'Accepting') 'successful command without policy readback is insufficient'
    $script:acceptanceIgnoreUpdate=$false
    [IO.File]::AppendAllText($s.Override,'synthetic tamper')
    $null=Expect-Failure {Invoke-ProdAccept $s} 'private configuration changed'
    Assert-Test (@($script:events | Where-Object {$_ -like 'update:*'}).Count -eq 0) 'pending acceptance refuses a third configuration hash'
  }
}
Test-Case 'phase-dependent readiness permits only expected policies and rollback restores originals after acceptance' {
  foreach($phase in @('Accepting','Accepted')){
    $s=New-TestAcceptanceState
    With-Mocks $script:acceptanceMocks {
      if($phase -ceq 'Accepted'){$null=Invoke-ProdAccept $s}else{
        $script:acceptanceFailure=$s.Replacements.'knowledge-service'
        $null=Expect-Failure {Invoke-ProdAccept $s} 'interrupted policy update';$script:acceptanceFailure=''
      }
      Assert-ReplacementConfiguration $s
      if($phase -ceq 'Accepted'){
        $entry=$script:containers[$s.Replacements.'open-webui'];$entry.Record.RestartPolicy=[pscustomobject]@{Name='no';MaximumRetryCount=0}
        $null=Expect-Failure {Assert-ReplacementConfiguration $s} 'restart policy mismatch'
        $entry.Record.RestartPolicy=[pscustomobject]@{Name='unless-stopped';MaximumRetryCount=0}
      }
      $null=Invoke-ProdRollback $s
      Assert-Test ($s.Phase -ceq 'RolledBack') 'rollback works after completed or interrupted acceptance'
      Assert-Restored $s
    }
  }
}
Test-Case 'acceptance network prerequisite validation is read-only and rejects unrecorded networks' {
  $s=New-TestAcceptanceState
  With-Mocks @{
    'Read-DockerJson'={param($Arguments)
      if($Arguments[-1] -ceq 'agent-internal'){return [pscustomobject]@{Id=$s.InternalNetwork}}
      [pscustomobject]@{Name='agent-production-egress';Id=$s.EgressId;Driver='bridge';Internal=$false;Labels=[pscustomobject]@{'io.agent-production.project'='agent-production'}}
    }
    'Invoke-ProdNative'={param($Kind,$Arguments,$Operation,$InputText)
      Assert-Test (($Arguments -join ' ') -ceq 'network ls -q --no-trunc --filter name=^agent-production-egress$') 'network check never creates or mutates a network';$s.EgressId
    }
    'Save-ProdState'={throw 'TEST: network validation must not mutate state before readiness'}
  } {
    Assert-ProductionNetworks $s -ReadOnly
    $s.EgressId=$null;$null=Expect-Failure {Assert-ProductionNetworks $s -ReadOnly} 'recorded existing production networks'
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
