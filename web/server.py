#!/usr/bin/env python3
"""BlockLab local control API for Paper worlds and datapack artifacts."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
import json, os, re, shutil, subprocess, threading, time, secrets, zipfile, signal

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
SERVER = ROOT / 'server'
GAMEMODES = ROOT / 'Gamemodes'
ARTIFACTS = ROOT / 'artifacts'
TOKEN_FILE = ROOT / '.blocklab-admin-token'
PORT = int(os.environ.get('BLOCKLAB_PORT', '8080'))
JAVA = os.environ.get('JAVA', '/usr/lib/jvm/java-25-openjdk-arm64/bin/java')
if not Path(JAVA).exists(): JAVA = shutil.which('java') or 'java'

if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(12))
    TOKEN_FILE.chmod(0o600)
ADMIN_CODE = TOKEN_FILE.read_text().strip()  # Non-secret identifier shown in the Actions panel.
ADMIN_PASSWORD = os.environ.get('BLOCKLAB_ADMIN_PASSWORD') or ADMIN_CODE
SESSIONS = set()
LOCK = threading.RLock()
STATE = {'status':'stopped','mode':None,'world':None,'message':'Chọn gamemode để bắt đầu.','error':None,'started_at':None}
PROC = None
LOG_HANDLE = None
LOG_PATH = SERVER / 'logs' / 'blocklab-console.log'


def available_modes():
    result=[]
    for p in sorted(GAMEMODES.glob('*.zip')):
        key=re.sub(r'[^a-z0-9_-]+','_',p.stem.lower()).strip('_')
        result.append({'id':key,'name':p.stem.replace('_',' ').replace('-',' ').title(),'datapack':p.name,'description':f'Gamemode từ {p.name}'})
    return result


def world_list():
    worlds=[]
    if SERVER.exists():
        for p in sorted(SERVER.iterdir()):
            if p.is_dir() and ((p/'level.dat').is_file() or (p/'datapacks').is_dir()) and p.name not in ('plugins','config','logs','cache','libraries'):
                meta=ROOT/'worlds.json'
                mode=''
                if meta.exists():
                    try: mode=json.loads(meta.read_text()).get(p.name,'')
                    except Exception: pass
                worlds.append({'name':p.name,'mode':mode,'artifact':(ARTIFACTS/(p.name+'.zip')).exists()})
    return worlds


def save_world_meta(world, mode):
    path=ROOT/'worlds.json'
    try: data=json.loads(path.read_text()) if path.exists() else {}
    except Exception: data={}
    data[world]=mode
    path.write_text(json.dumps(data,indent=2))


def safe_world_name(value):
    name=re.sub(r'[^a-zA-Z0-9_-]+','-',str(value or '').strip()).strip('-_').lower()
    if not name or len(name)>40 or name in ('.','..'):
        raise ValueError('Tên world không hợp lệ (1–40 ký tự: a-z, 0-9, _ hoặc -).')
    return name


def set_property(key, value):
    path=SERVER/'server.properties'
    lines=path.read_text().splitlines() if path.exists() else []
    prefix=key+'='
    out=[]; found=False
    for line in lines:
        if line.startswith(prefix):
            if not found: out.append(prefix+str(value)); found=True
        else: out.append(line)
    if not found: out.append(prefix+str(value))
    path.write_text('\n'.join(out)+'\n')


def install_datapack(world, mode):
    pack=next((m for m in available_modes() if m['id']==mode),None)
    if not pack: raise ValueError('Gamemode này chưa có datapack trong Gamemodes/.')
    datapacks=SERVER/world/'datapacks'
    datapacks.mkdir(parents=True,exist_ok=True)
    source=GAMEMODES/pack['datapack']
    target=datapacks/pack['datapack']
    shutil.copy2(source,target)
    return pack


def export_world(world):
    source=SERVER/world
    if not (source/'level.dat').exists():
        return None
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    target=ARTIFACTS/(world+'.zip')
    temp=ARTIFACTS/(world+'.zip.tmp')
    with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED,compresslevel=4) as z:
        for p in source.rglob('*'):
            if not p.is_file() or p.name=='session.lock': continue
            z.write(p,p.relative_to(SERVER))
    temp.replace(target)
    return {'name':target.name,'size':target.stat().st_size,'updated':int(target.stat().st_mtime)}


def stop_server():
    global PROC, LOG_HANDLE
    with LOCK:
        proc=PROC
        if not proc or proc.poll() is not None:
            PROC=None
            if STATE['world']:
                export_world(STATE['world'])
            STATE.update(status='stopped',message='Server đã dừng.',started_at=None)
            return
        STATE.update(status='stopping',message='Đang lưu world và dừng Paper...')
        try:
            proc.stdin.write('save-all flush\n'); proc.stdin.flush()
            time.sleep(2)
            proc.stdin.write('stop\n'); proc.stdin.flush()
        except Exception: pass
    try: proc.wait(timeout=45)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
    with LOCK:
        PROC=None
        if LOG_HANDLE:
            try: LOG_HANDLE.close()
            except Exception: pass
            LOG_HANDLE=None
        old_world=STATE.get('world')
        artifact=export_world(old_world) if old_world else None
        STATE.update(status='stopped',message='Server đã dừng; world đã lưu.' if artifact else 'Server đã dừng.',started_at=None)
        if artifact: STATE['message'] += f' Artifact: {artifact["name"]}'


def launch_worker(world, mode):
    global PROC, LOG_HANDLE
    try:
        with LOCK:
            if PROC and PROC.poll() is None:
                if STATE.get('world')==world:
                    STATE.update(status='online',message='World đang chạy.'); return
                stop_server()
            STATE.update(status='preparing',world=world,mode=mode,message='Đang cài datapack và chuẩn bị world...',error=None,started_at=None)
        pack=install_datapack(world,mode)
        save_world_meta(world,mode)
        set_property('level-name',world)
        set_property('server-port',os.environ.get('MINECRAFT_PORT','25565'))
        set_property('motd',f'BlockLab | {world}')
        (SERVER/'eula.txt').write_text('eula=true\n')
        (SERVER/'logs').mkdir(parents=True,exist_ok=True)
        with LOCK:
            STATE.update(status='starting',message=f'Đang khởi động Paper · {world}')
            LOG_HANDLE=open(LOG_PATH,'w',buffering=1)
            proc=subprocess.Popen([JAVA,'-Xms512M','-Xmx2G','-jar',str(SERVER/'paper.jar'),'--nogui'],cwd=SERVER,stdin=subprocess.PIPE,stdout=LOG_HANDLE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            PROC=proc
        deadline=time.time()+180
        while time.time()<deadline:
            if proc.poll() is not None:
                tail=LOG_PATH.read_text(errors='replace')[-6000:] if LOG_PATH.exists() else ''
                raise RuntimeError('Paper thoát khi khởi động. Kiểm tra Java/Paper. '+tail[-900:])
            log=LOG_PATH.read_text(errors='replace')[-12000:] if LOG_PATH.exists() else ''
            if 'Done (' in log:
                with LOCK: STATE.update(status='online',message=f'World {world} đã chạy. Datapack: {pack["datapack"]}',started_at=int(time.time()),error=None)
                return
            time.sleep(1)
        raise RuntimeError('Paper không sẵn sàng sau 180 giây; xem logs/blocklab-console.log.')
    except Exception as exc:
        with LOCK:
            STATE.update(status='error',message='Không thể khởi động server.',error=str(exc))
            proc=PROC
        if proc and proc.poll() is None:
            try: proc.terminate()
            except Exception: pass
        PROC=None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(WEB),**kwargs)
    def log_message(self,fmt,*args): print('[web]',fmt%args,flush=True)
    def send_json(self,code,obj,headers=None):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)
    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        return json.loads(self.rfile.read(min(n,10000)) or b'{}')
    def authorized(self):
        cookie=self.headers.get('Cookie','')
        token=next((x.strip().split('=',1)[1] for x in cookie.split(';') if x.strip().startswith('blocklab_session=')),None)
        return bool(token and token in SESSIONS)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/status':
            with LOCK: state=dict(STATE)
            state['modes']=available_modes(); state['worlds']=world_list(); state['minecraft_address']=Path(ROOT/'minecraft-address.txt').read_text().strip() if (ROOT/'minecraft-address.txt').exists() else f'{os.environ.get("MINECRAFT_HOST","127.0.0.1")}:{os.environ.get("MINECRAFT_PORT","25565")} (local only)'
            state['tunnel_url']=Path(ROOT/'web-url.txt').read_text().strip() if (ROOT/'web-url.txt').exists() else ''
            state['artifacts']=[{'name':p.name,'size':p.stat().st_size,'updated':int(p.stat().st_mtime)} for p in sorted(ARTIFACTS.glob('*.zip'))]
            return self.send_json(200,state)
        if path.startswith('/api/'):
            return self.send_json(404,{'error':'Không tìm thấy API.'})
        if path.startswith('/artifacts/'):
            name=Path(path.removeprefix('/artifacts/')).name
            target=ARTIFACTS/name
            if not name.endswith('.zip') or not target.is_file(): return self.send_error(404)
            self.send_response(200); self.send_header('Content-Type','application/zip'); self.send_header('Content-Length',str(target.stat().st_size)); self.send_header('Content-Disposition',f'attachment; filename=\"{name}\"'); self.end_headers()
            with target.open('rb') as f: shutil.copyfileobj(f,self.wfile)
            return
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        try: data=self.body()
        except Exception: return self.send_json(400,{'error':'JSON không hợp lệ.'})
        if path=='/api/login':
            if data.get('username','').strip().lower()!='tonkun' or not secrets.compare_digest(str(data.get('password','')),ADMIN_PASSWORD):
                return self.send_json(401,{'error':'Tên tài khoản hoặc mật khẩu không đúng.'})
            sid=secrets.token_urlsafe(32); SESSIONS.add(sid)
            return self.send_json(200,{'ok':True,'username':'TonKun'},{'Set-Cookie':f'blocklab_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
        if path=='/api/logout':
            cookie=self.headers.get('Cookie','')
            for item in cookie.split(';'):
                if item.strip().startswith('blocklab_session='): SESSIONS.discard(item.strip().split('=',1)[1])
            return self.send_json(200,{'ok':True},{'Set-Cookie':'blocklab_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if not self.authorized(): return self.send_json(401,{'error':'Phiên đăng nhập hết hạn.'})
        if path=='/api/start':
            mode=str(data.get('mode','')); world=safe_world_name(data.get('world','world-'+mode.replace('_','-')))
            if mode not in [m['id'] for m in available_modes()]: return self.send_json(400,{'error':'Gamemode chưa có datapack.'})
            with LOCK:
                if STATE['status'] in ('preparing','starting','stopping'): return self.send_json(409,{'error':'Server đang chuyển trạng thái, chờ một chút.'})
                if PROC and PROC.poll() is None and STATE.get('world')==world and STATE.get('mode')==mode: return self.send_json(200,{'ok':True,'message':'World đã chạy.'})
                STATE.update(status='preparing',message='Đang chuẩn bị world...',error=None)
            threading.Thread(target=launch_worker,args=(world,mode),daemon=True).start()
            return self.send_json(202,{'ok':True,'message':'Đã nhận yêu cầu khởi động.'})
        if path=='/api/stop':
            threading.Thread(target=stop_server,daemon=True).start()
            return self.send_json(202,{'ok':True,'message':'Đang dừng server an toàn.'})
        if path=='/api/export':
            with LOCK:
                if PROC and PROC.poll() is None: return self.send_json(409,{'error':'Dừng server trước khi xuất artifact để tránh ZIP hỏng.'})
                world=safe_world_name(data.get('world',STATE.get('world') or 'world'))
                result=export_world(world)
            if not result: return self.send_json(404,{'error':'World chưa được tạo.'})
            return self.send_json(200,{'ok':True,'artifact':result})
        if path=='/api/create-world':
            name=safe_world_name(data.get('world','')); mode=str(data.get('mode',''))
            if mode not in [m['id'] for m in available_modes()]: return self.send_json(400,{'error':'Gamemode chưa có datapack.'})
            if name in [w['name'] for w in world_list()]: return self.send_json(409,{'error':'Tên world đã tồn tại.'})
            (SERVER/name).mkdir(parents=True,exist_ok=True); install_datapack(name,mode); save_world_meta(name,mode)
            return self.send_json(201,{'ok':True,'world':{'name':name,'mode':mode}})
        return self.send_json(404,{'error':'Không tìm thấy API.'})

def shutdown_handler(signum, frame):
    try: stop_server()
    finally: raise SystemExit(0)

if __name__=='__main__':
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    print(f'BlockLab root: {ROOT}',flush=True)
    print(f'Admin username: TonKun',flush=True)
    print(f'Admin ID: {ADMIN_CODE}',flush=True)
    print(f'Java: {JAVA}',flush=True)
    print(f'Gamemodes: {[m["id"] for m in available_modes()]}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
