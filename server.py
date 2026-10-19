#!/usr/bin/env python3
"""Local LM Studio telemetry. Python standard library only; never stores prompts."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

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
        self.last_sample = 0

    def sample(self, data):
        with self.lock:
            if data['ts'] - self.last_sample < 5:
                return
            self.last_sample = data['ts']
            self.db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?)', (data['ts'], json.dumps(data)))
            self.db.execute('DELETE FROM samples WHERE ts < ?', (data['ts'] - 86400,))
            self.db.execute('DELETE FROM requests WHERE ts < ?', (data['ts'] - 7 * 86400,))
            self.db.commit()

    def request(self, ts, model, stats):
        with self.lock:
            cur = self.db.execute('INSERT INTO requests(ts,model,data) VALUES (?,?,?)', (ts, model, json.dumps(stats)))
            self.db.commit()
            return cur.lastrowid

    def requests(self):
        with self.lock:
            return [dict(id=i, ts=t, model=m, **json.loads(d)) for i,t,m,d in self.db.execute('SELECT id,ts,model,data FROM requests ORDER BY id DESC LIMIT 100')]

    def history(self, seconds):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM samples WHERE ts >= ? ORDER BY ts', (time.time()-seconds,))]

class Monitor:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.RLock()
        self.state = {'ts': time.time(), 'models': [], 'gpu': [], 'gpu_processes': [], 'system': {}, 'live': {}, 'requests': store.requests(), 'sources': {}, 'lm_connected': False}
        self.children = set()
        self.children_lock = threading.Lock()
        self.last_cpu = None
    def source(self, key, ok, detail):
        with self.lock:
            self.state['sources'][key] = {'ok': ok, 'detail': detail, 'ts': time.time()}

    def snapshot(self):
        with self.lock:
            data = json.loads(json.dumps(self.state))
        data['ts'] = time.time()
        for live in data['live'].values():
            live['stale'] = time.time()-live.get('updated_at',0) > 12
        return data

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
                    for model, live in self.state['live'].items():
                        if model not in loaded:
                            live['active'] = False
                            for slot in live.get('slots', {}).values():
                                slot['active'] = False
                self.source('lm_api', True, '原生 API · 每 3 秒')
            except Exception as exc:
                with self.lock:
                    self.state['lm_connected'] = False
                    for live in self.state['live'].values():
                        live['active'] = False
                detail = '无法连接；请启动 LM Studio API' if isinstance(exc, urllib.error.URLError) else str(exc)
                if isinstance(exc, urllib.error.HTTPError):
                    detail = 'HTTP '+str(exc.code)+'；检查 API 版本或 LM_API_TOKEN'
                self.source('lm_api', False, detail)
            STOP.wait(3)

    def process_event(self, source, event):
        data = event.get('data', {})
        model = data.get('modelIdentifier')
        if not model:
            return
        ts = number(event.get('timestamp'))
        ts = ts/1000 if ts else time.time()
        with self.lock:
            live = self.state['live'].setdefault(model, {})
            if source == 'model' and data.get('type') == 'llm.prediction.output':
                raw = data.get('stats', {})
                stats = {key:raw.get(key) for key in ('stopReason','tokensPerSecond','timeToFirstTokenSec','totalTimeSec','promptTokensCount','predictedTokensCount','totalTokensCount')}
                row = dict(id=self.store.request(ts, model, stats), ts=ts, model=model, **stats)
                self.state['requests'] = ([row]+self.state['requests'])[:100]
                live['last_stats'] = stats
                live['summary_at'] = ts
                return
            if source != 'runtime':
                return
            message = data.get('message', '')
            live.update(pid=data.get('pid'), engine=data.get('engineName'), engine_version=data.get('engineVersion'))
            key = '0'
            for line in message.splitlines():
                slot = re.search(r'id\s+(\d+)\s*\|\s*task\s+(-?\d+)',line)
                key = slot.group(1) if slot else key
                slots = live.setdefault('slots',{})
                state = slots.setdefault(key,{})
                timing = re.search(r'n_gen\s*=\s*(\d+),\s*tg\s*=\s*([\d.]+)\s*t/s,\s*tg_3s\s*=\s*([\d.]+)',line)
                if 'processing task' in line or timing:
                    task = slot.group(2) if slot else None
                    if task != state.get('task'):
                        state.clear()
                    state.update(active=True, task=task, updated_at=ts)
                if timing:
                    state.update(generated_tokens=int(timing[1]), decode_tps=float(timing[3]), average_tps=float(timing[2]))
                prefill = re.search(r'prompt eval time\s*=\s*([\d.]+) ms\s*/\s*(\d+) tokens.*?([\d.]+) tokens per second',line)
                if prefill:
                    state.update(prefill_ms=float(prefill[1]), prefill_tokens=int(prefill[2]), prefill_tps=float(prefill[3]))
                finish = re.search(r'stop processing: n_tokens\s*=\s*(\d+), truncated\s*=\s*(\d+)',line)
                if finish:
                    state.update(active=False, context_tokens=int(finish[1]), truncated=bool(int(finish[2])), updated_at=ts)
                if timing or prefill or finish or 'processing task' in line:
                    live['updated_at'] = ts
            live['active'] = any(s.get('active',False) for s in live.get('slots',{}).values())

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
            try:
                args = [self.config.lms,'log','stream','--source',source,'--json']
                if source == 'model':
                    args += ['--stats','--filter','output']
                process = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
                with self.children_lock:
                    self.children.add(process)
                self.source(source, True, '日志连接已启动 · 自动重连')
                for line in process.stdout:
                    if STOP.is_set():
                        break
                    try:
                        self.process_event(source,json.loads(line))
                        self.source(source,True,'日志事件已接收')
                    except (json.JSONDecodeError, TypeError, ValueError):
                        if 'error' in line.lower():
                            self.source(source,False,'日志流错误；等待重连')
                if not STOP.is_set():
                    self.source(source,False,'日志流断开；3 秒后重连')
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
            STOP.wait(3)

    def gpu_read(self):
        fields='index,uuid,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem'
        result=subprocess.run(['nvidia-smi','--query-gpu='+fields,'--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3,check=True)
        import csv
        keys=['index','uuid','name','utilization','memory_utilization','memory_used','memory_total','temperature','power','sm_clock','memory_clock']
        devices=[]
        for row in csv.reader(result.stdout.splitlines(),skipinitialspace=True):
            d=dict(zip(keys,row))
            for k in keys:
                if k not in ('uuid','name'):
                    d[k]=number(d[k])
            devices.append(d)
        apps=subprocess.run(['nvidia-smi','--query-compute-apps=gpu_uuid,pid,process_name,used_memory','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3)
        processes=[]
        if apps.returncode == 0:
            for row in csv.reader(apps.stdout.splitlines(),skipinitialspace=True):
                if len(row)==4:
                    processes.append(dict(gpu_uuid=row[0],pid=number(row[1]),name=row[2],memory=number(row[3])))
        return devices, processes

    def system_read(self):
        info={}
        for line in Path('/proc/meminfo').read_text().splitlines():
            k,v=line.split(':',1); info[k]=int(v.strip().split()[0])
        mem_total=info['MemTotal']/1024
        ticks=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
        total=sum(ticks[:8]); idle=ticks[3]+ticks[4]
        cpu=None
        if self.last_cpu and total>self.last_cpu[0]:
            cpu=100*(1-(idle-self.last_cpu[1])/(total-self.last_cpu[0]))
        self.last_cpu=(total,idle)
        return dict(cpu=cpu,memory_total=mem_total,memory_used=mem_total-info['MemAvailable']/1024)

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
                self.source('system',True,'Linux /proc · 每秒')
            except (OSError,ValueError,KeyError):
                self.source('system',False,'系统采样不可用')
            state=self.snapshot()
            active=[s for live in state['live'].values() if state['lm_connected'] and not live['stale'] for s in live.get('slots',{}).values() if s.get('active') and time.time()-s.get('updated_at',0)<12]
            self.store.sample(dict(ts=time.time(),gpu=state['gpu'],system=state['system'],decode_tps=sum(s.get('decode_tps',0) for s in active) if any(s.get('decode_tps') is not None for s in active) else None))
            STOP.wait(max(.05,1-(time.monotonic()-start)))

    def start(self):
        for target,args in [(self.poll_models,()),(self.sampler,()),(self.log_worker,('model',)),(self.log_worker,('runtime',))]:
            threading.Thread(target=target,args=args,daemon=True).start()

    def stop(self):
        STOP.set()
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
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/api/state':
            self.send(200,json.dumps(self.server.monitor.snapshot()).encode(),'application/json; charset=utf-8')
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
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
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
    parser.add_argument('--lms',default=os.environ.get('LMS_BIN',str(Path.home()/'.lmstudio/bin/lms')))
    parser.add_argument('--db',default=str(ROOT/'data'/'monitor.sqlite3'))
    config=parser.parse_args();config.lm_url=config.lm_url.rstrip('/');config.token=os.environ.get('LM_API_TOKEN','')
    url=urlsplit(config.lm_url)
    if url.hostname not in ('localhost','127.0.0.1','::1') or url.scheme not in ('http','https'):
        parser.error('第一版仅支持本机 LM Studio URL，CLI 日志始终来自本机')
    Path(config.db).parent.mkdir(parents=True,exist_ok=True)
    store=Store(config.db);monitor=Monitor(config,store)
    server=ThreadingHTTPServer((config.host,config.port),Handler);server.daemon_threads=True;server.monitor=monitor
    def shutdown(*_):
        monitor.stop()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    monitor.start()
    print(f'LM Studio Monitor: http://{config.host}:{config.port}',flush=True)
    try:
        server.serve_forever(poll_interval=.3)
    finally:
        monitor.stop(); server.server_close()

if __name__=='__main__':
    main()
