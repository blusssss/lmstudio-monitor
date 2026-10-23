"""Local LM Studio telemetry for Windows. Standard library only; never stores prompts."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from telemetry import Tasks, TERMINAL
from platform_support import find_lms, find_nvidia_smi, child_options, windows_system, lms_connection_args, lms_environment

ROOT = Path(__file__).resolve().parent
STOP = threading.Event()

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None

class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL, model TEXT, data TEXT)')
        self.db.commit()
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(requests)')}
        if 'request_key' not in columns:
            self.db.execute('ALTER TABLE requests ADD COLUMN request_key TEXT')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS request_key_index ON requests(request_key)')
        self.db.execute('CREATE INDEX IF NOT EXISTS requests_time_index ON requests(ts)')
        self.db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.commit()
        revision = self.db.execute("SELECT value FROM settings WHERE key='history_revision'").fetchone()
        self.history_revision = float(revision[0]) if revision else 0
        self.last_sample = 0

    def sample(self, data):
        with self.lock:
            if data.get('history_revision',self.history_revision) != self.history_revision:
                return
            if data['ts'] < self.history_revision:
                return
            if data['ts'] - self.last_sample < 5:
                return
            self.last_sample = data['ts']
            self.db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?)', (data['ts'], json.dumps(data)))
            self.db.execute('DELETE FROM samples WHERE ts < ?', (data['ts'] - 86400,))
            self.db.execute('DELETE FROM requests WHERE ts < ?', (data['ts'] - 7 * 86400,))
            self.db.commit()

    def request(self, ts, model, stats):
        fingerprint = hashlib.sha256(json.dumps([ts,model,stats],sort_keys=True).encode()).hexdigest()
        key = 'summary:'+fingerprint
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO requests(ts,model,data,request_key) VALUES (?,?,?,?)', (ts,model,json.dumps(stats),key))
            self.db.commit()
            return self.db.execute('SELECT id FROM requests WHERE request_key=?',(key,)).fetchone()[0]

    def task(self, row):
        with self.lock:
            self.db.execute("INSERT INTO requests(ts,model,data,request_key) VALUES (?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET ts=excluded.ts,data=excluded.data", (row['updated_at'],row['model'],json.dumps(row),row['request_key']))
            self.db.commit()

    def recover(self):
        with self.lock:
            rows = list(self.db.execute('SELECT id,data FROM requests WHERE request_key IS NOT NULL'))
            for ident, data in rows:
                row = json.loads(data)
                if row.get('kind') == 'runtime' and row.get('status') not in TERMINAL:
                    row.update(status='interrupted', ended_at=time.time(), updated_at=time.time(), stopReason='采集器重启，后续状态未知', decode_tps=None)
                    row.setdefault('stage_events',[]).append({'phase':'interrupted','ts':row['ended_at']})
                    self.db.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(row),ident))
            self.db.commit()

    def clear_history(self, active_keys):
        with self.lock:
            before_requests = self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            before_samples = self.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
            revision = time.time()
            with self.db:
                self.db.execute('DELETE FROM samples')
                if active_keys:
                    placeholders = ','.join('?' for _ in active_keys)
                    self.db.execute(f'DELETE FROM requests WHERE request_key IS NULL OR request_key NOT IN ({placeholders})',tuple(active_keys))
                else:
                    self.db.execute('DELETE FROM requests')
                self.db.execute("INSERT INTO settings VALUES ('history_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(revision),))
            self.history_revision = revision
            self.last_sample = 0
            remaining = self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            return dict(requests_deleted=before_requests-remaining,samples_deleted=before_samples,active_preserved=remaining,history_revision=revision)

    def requests(self, kind=None):
        with self.lock:
            rows = [{**json.loads(d), 'id':i, 'ts':t, 'model':m} for i,t,m,d in self.db.execute('SELECT id,ts,model,data FROM requests ORDER BY ts DESC LIMIT 1000')]
            return [r for r in rows if kind is None or r.get('kind','summary')==kind][:200]

    def history(self, seconds):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM samples WHERE ts >= ? ORDER BY ts', (time.time()-seconds,))]

class Monitor:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.RLock()
        self.state = {'ts': time.time(), 'models': [], 'gpu': [], 'gpu_processes': [], 'system': {}, 'live': {}, 'requests': [], 'sources': {}, 'lm_connected': False}
        self.children = set()
        self.children_lock = threading.Lock()
        self.last_cpu = None
        store.recover()
        self.tasks = Tasks(store.task)
        self.threads = []
        self.diagnostics = []

    def source(self, key, ok, detail, status=None, event=False):
        with self.lock:
            previous = self.state['sources'].get(key,{})
            self.state['sources'][key] = {'ok':ok, 'status':status or ('ready' if ok else 'disconnected'), 'detail':detail, 'ts':time.time(), 'last_event_at':time.time() if event else previous.get('last_event_at')}

    def diagnostic(self, code):
        with self.lock:
            self.diagnostics = ([{'ts':time.time(),'code':code}] + self.diagnostics)[:20]

    def snapshot(self):
        with self.lock:
            data = json.loads(json.dumps(self.state))
            data['ts'] = time.time()
            data['version'] = '2.0.0'
            data['history_revision'] = self.store.history_revision
            data['live'] = self.tasks.live(data['lm_connected'],data['sources'].get('runtime',{}),data['ts'])
            data['requests'] = self.store.requests('runtime')
            data['summaries'] = self.store.requests('summary')
            data['diagnostics'] = list(self.diagnostics)
        return data

    def clear_history(self):
        with self.lock:
            active = {key:row for key,row in self.tasks.tasks.items() if row['status'] not in TERMINAL}
            result = self.store.clear_history(list(active))
            self.tasks.tasks = active
            self.tasks.slots = {key:value for key,value in self.tasks.slots.items() if value in active}
            self.diagnostics = []
            return {**result,'state':self.snapshot()}

    def disconnected(self, reason):
        with self.lock:
            self.state['lm_connected'] = False
            self.tasks.interrupt(reason)

    def poll_models(self):
        while not STOP.is_set():
            try:
                headers = {'Authorization': 'Bearer '+self.config.token} if self.config.token else {}
                req = urllib.request.Request(self.config.lm_url+'/api/v1/models', headers=headers)
                with urllib.request.urlopen(req, timeout=2) as r:
                    result = json.load(r)
                models = result.get('models')
                if not isinstance(models,list):
                    raise ValueError('接口未返回 models 数组，请检查 LM Studio 版本')
                with self.lock:
                    self.state['models'] = models
                    self.state['lm_connected'] = True
                    loaded = {i['id'] for m in models for i in m.get('loaded_instances',[])}
                    for model in {r['model'] for r in self.tasks.tasks.values()} - loaded:
                        self.tasks.interrupt('模型已卸载',model)
                self.source('lm_api', True, '原生 API · 每 3 秒')
            except Exception as exc:
                self.disconnected('LM Studio API 连接中断，后续状态未知')
                detail = '无法连接；请启动 LM Studio API' if isinstance(exc, urllib.error.URLError) else str(exc)
                if isinstance(exc, urllib.error.HTTPError):
                    detail = 'HTTP '+str(exc.code)+'；检查 API 版本或 LM_API_TOKEN'
                self.source('lm_api', False, detail)
            STOP.wait(3)

    def process_event(self, source, event):
        if not isinstance(event,dict):
            raise ValueError('invalid event')
        data = event.get('data', {})
        if not isinstance(data,dict):
            return
        model = data.get('modelIdentifier')
        if not model:
            return
        ts = number(event.get('timestamp'))
        ts = ts/1000 if ts else time.time()
        with self.lock:
            if ts <= self.store.history_revision:
                return
            if source == 'model' and data.get('type') == 'llm.prediction.output':
                raw = data.get('stats') or {}
                stats = {key:raw.get(key) for key in ('stopReason','tokensPerSecond','timeToFirstTokenSec','totalTimeSec','promptTokensCount','predictedTokensCount','totalTokensCount')}
                stats['kind'] = 'summary'
                # No request ID is exposed: never merge a summary into a Runtime task by timing.
                stats['association'] = 'unlinked'
                self.store.request(ts,model,stats)
            elif source == 'runtime':
                data = dict(data)
                for item in self.state['models']:
                    for instance in item.get('loaded_instances',[]):
                        if instance.get('id') == model:
                            data['_context_capacity'] = instance.get('config',{}).get('context_length')
                self.tasks.process(model,data,ts)

    def log_worker(self, source):
        # CLI attaches to the local daemon; does not launch it. No raw IO is persisted.
        while not STOP.is_set():
            with self.lock:
                connected = self.state['lm_connected']
            if not connected:
                self.source(source,False,'等待本机 LM Studio API')
                STOP.wait(3)
                continue
            process = None
            context = ExitStack()
            try:
                args = [self.config.lms,'log','stream','--source',source,'--json']
                connection_args = lms_connection_args(getattr(self.config,'lms_port',None))
                args += connection_args
                if source == 'model':
                    args += ['--stats','--filter','output']
                process = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env=context.enter_context(lms_environment(connection_args)),**child_options())
                with self.children_lock:
                    self.children.add(process)
                self.source(source, False, '正在连接日志服务',status='connecting')
                for line in process.stdout:
                    if STOP.is_set():
                        break
                    if 'Streaming logs from LM Studio' in line:
                        self.source(source,True,'已订阅，等待事件',status='subscribed')
                        continue
                    if 'Waking up LM Studio service' in line:
                        self.source(source,False,'CLI 未找到日志服务；检查 SDK 端口或 --lms-port',status='connecting')
                        continue
                    try:
                        self.process_event(source,json.loads(line))
                        self.source(source,True,'事件已接收',status='receiving',event=True)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        if line.lstrip().startswith('{'):
                            self.diagnostic(source+'_invalid_event')
                        if 'error' in line.lower():
                            self.source(source,False,'日志流错误；等待重连')
                            break
                if not STOP.is_set():
                    self.source(source,False,'订阅已断开，将自动重连')
                    if source == 'runtime':
                        with self.lock:
                            self.tasks.interrupt('Runtime 日志中断，后续状态未知')
            except (OSError, AttributeError):
                self.source(source,False,'无法启动 lms；检查 LMS_BIN')
            finally:
                if process:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                    if process.stdout:
                        process.stdout.close()
                    with self.children_lock:
                        self.children.discard(process)
                context.close()
            STOP.wait(3)

    def gpu_read(self):
        fields='index,uuid,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem'
        executable=find_nvidia_smi()
        result=subprocess.run([executable,'--query-gpu='+fields,'--format=csv,noheader,nounits'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=3,check=True,**child_options())
        import csv
        keys=['index','uuid','name','utilization','memory_utilization','memory_used','memory_total','temperature','power','sm_clock','memory_clock']
        devices=[]
        for row in csv.reader(result.stdout.splitlines(),skipinitialspace=True):
            d=dict(zip(keys,row))
            for k in keys:
                if k not in ('uuid','name'):
                    d[k]=number(d[k])
            devices.append(d)
        apps=subprocess.run([executable,'--query-compute-apps=gpu_uuid,pid,process_name,used_memory','--format=csv,noheader,nounits'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=3,**child_options())
        processes=[]
        if apps.returncode == 0:
            for row in csv.reader(apps.stdout.splitlines(),skipinitialspace=True):
                if len(row)==4:
                    processes.append(dict(gpu_uuid=row[0],pid=number(row[1]),name=row[2],memory=number(row[3])))
        return devices, processes

    def system_read(self):
        total,idle,mem_total,mem_used=windows_system()
        return self.system_values(total,idle,mem_total,mem_used)

    def system_values(self,total,idle,mem_total,mem_used):
        cpu=None
        if self.last_cpu and total>self.last_cpu[0]:
            cpu=max(0,min(100,100*(1-(idle-self.last_cpu[1])/(total-self.last_cpu[0]))))
        self.last_cpu=(total,idle)
        return dict(cpu=cpu,memory_total=mem_total,memory_used=mem_used)

    def sampler(self):
        while not STOP.is_set():
            start=time.monotonic()
            try:
                devices,apps=self.gpu_read()
                with self.lock:
                    self.state['gpu'],self.state['gpu_processes']=devices,apps
                self.source('gpu',True,'nvidia-smi · 约每秒')
            except (OSError, subprocess.SubprocessError):
                with self.lock:
                    self.state['gpu']=[];self.state['gpu_processes']=[]
                self.source('gpu',False,'GPU 采样失败或 nvidia-smi 不可用')
            try:
                system=self.system_read()
                with self.lock:
                    self.state['system']=system
                self.source('system',True,'Windows API · 每秒')
            except (OSError,ValueError,KeyError):
                self.source('system',False,'系统采样不可用')
            state=self.snapshot()
            active=[s for live in state['live'].values() if state['lm_connected'] for s in live.get('slots',{}).values() if s.get('active') and not s.get('stale')]
            self.store.sample(dict(ts=time.time(),history_revision=state['history_revision'],gpu=state['gpu'],system=state['system'],instances={model: {'decode_tps': sum(s['decode_tps'] for s in live['slots'].values() if s.get('decode_tps') is not None) if any(s.get('decode_tps') is not None for s in live['slots'].values()) else None} for model,live in state['live'].items()},decode_tps=sum(s.get('decode_tps',0) for s in active) if any(s.get('decode_tps') is not None for s in active) else None))
            STOP.wait(max(.05,1-(time.monotonic()-start)))

    def start(self):
        for target,args in [(self.poll_models,()),(self.sampler,()),(self.log_worker,('model',)),(self.log_worker,('runtime',))]:
            thread = threading.Thread(target=target,args=args,daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        STOP.set()
        with self.lock:
            self.tasks.interrupt('采集器已停止')
        with self.children_lock:
            for child in self.children:
                if child.poll() is None:
                    child.terminate()

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):
        pass
    def send(self,status,body,content_type):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        if urlsplit(self.path).path != '/api/history/clear':
            self.send(404,b'Not found','text/plain')
            return
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            self.send(403,b'Origin rejected','text/plain')
            return
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            self.send(415,b'JSON required','text/plain')
            return
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 1024:
                raise ValueError()
            if not isinstance(json.loads(self.rfile.read(size)),dict):
                raise ValueError()
        except (ValueError,TypeError):
            self.send(400,b'Invalid JSON','text/plain')
            return
        self.send(200,json.dumps(self.server.monitor.clear_history()).encode(),'application/json; charset=utf-8')

    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/api/state':
            self.send(200,json.dumps(self.server.monitor.snapshot()).encode(),'application/json; charset=utf-8')
        elif path=='/api/diagnostics':
            state=self.server.monitor.snapshot()
            body={k:state[k] for k in ('version','ts','lm_connected','sources','diagnostics')}
            self.send(200,json.dumps(body).encode(),'application/json; charset=utf-8')
        elif path=='/api/history':
            self.send(200,json.dumps(self.server.monitor.store.history(86400)).encode(),'application/json; charset=utf-8')
        elif path=='/api/events':
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-cache')
            self.send_header('Connection','close')
            self.send_header('X-Accel-Buffering','no')
            self.end_headers()
            self.connection.settimeout(5)
            try:
                while not STOP.is_set():
                    self.wfile.write(('data: '+json.dumps(self.server.monitor.snapshot())+'\n\n').encode()); self.wfile.flush()
                    STOP.wait(1)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):
                pass
            self.close_connection=True
        elif path in ('/','/app.js','/style.css'):
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[path]
            mime={'html':'text/html','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
            self.send(200,(ROOT/'static'/name).read_bytes(),mime+'; charset=utf-8')
        else:
            self.send(404,b'Not found','text/plain')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8090)
    parser.add_argument('--lm-url',default=os.environ.get('LM_URL','http://127.0.0.1:1234'))
    parser.add_argument('--lms',default=find_lms())
    parser.add_argument('--lms-port',type=int,help='本机 CLI/SDK 端口（不是 REST API 端口）；默认自动发现')
    parser.add_argument('--db',default=str(ROOT/'data'/'monitor.sqlite3'))
    config=parser.parse_args();config.lm_url=config.lm_url.rstrip('/');config.token=os.environ.get('LM_API_TOKEN','')
    if os.name != 'nt':
        parser.error('This version supports Windows 10/11 only.')
    if config.lms_port is not None and not 0 < config.lms_port < 65536:
        parser.error('--lms-port 必须在 1 到 65535 之间')
    url=urlsplit(config.lm_url)
    if url.hostname not in ('localhost','127.0.0.1','::1') or url.scheme not in ('http','https'):
        parser.error('仅支持本机 LM Studio URL，CLI 日志始终来自本机')
    Path(config.db).parent.mkdir(parents=True,exist_ok=True)
    store=Store(config.db);monitor=Monitor(config,store)
    server=ThreadingHTTPServer((config.host,config.port),Handler);server.daemon_threads=True;server.monitor=monitor
    def shutdown(*_):
        monitor.stop()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    if hasattr(signal,'SIGBREAK'):
        signal.signal(signal.SIGBREAK,shutdown)
    monitor.start()
    print(f'LM Studio Monitor: http://{config.host}:{config.port}',flush=True)
    try:
        server.serve_forever(poll_interval=.3)
    finally:
        monitor.stop()
        server.server_close()
        for thread in monitor.threads:
            thread.join(timeout=7)
        if all(not thread.is_alive() for thread in monitor.threads):
            store.db.close()

if __name__=='__main__':
    main()
