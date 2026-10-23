"""Opt-in destructive-to-test-instance check: shuts down local test LM Studio daemon."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from platform_support import find_lms, child_options

LMS=find_lms()
def command(*args):
    result=subprocess.run([LMS,*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=40,**child_options())
    if result.returncode:raise RuntimeError('lms command failed: '+str(args[:2]))
def state():return json.load(urllib.request.urlopen('http://127.0.0.1:8090/api/state',timeout=4))
def wait_until(check,timeout=15):
    start=time.time()
    while time.time()-start<timeout:
        value=state()
        if check(value):return value
        time.sleep(.3)
    raise AssertionError('Lifecycle condition timed out')
def predict(model,limit):
    req=urllib.request.Request('http://127.0.0.1:1234/api/v1/chat',data=json.dumps({'model':model,'input':'Explain computer architecture and inference performance in at least 2000 words. /no_think','reasoning':'off','stream':True,'max_output_tokens':limit}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=40) as r:
        for line in r:
            if line.startswith(b'data:'):
                e=json.loads(line[5:])
                if e.get('type')=='chat.end':return e['result']['stats']
result=[]
def interrupted_request():
    try:predict('v2-validation',2048)
    except Exception as e:result.append(type(e).__name__)
start=time.time();thread=threading.Thread(target=interrupted_request);thread.start()
wait_until(lambda s:any(r['first_observed_at']>=start and r['status']=='decode' for r in s['requests']))
command('daemon','down');thread.join(timeout=10)
assert not thread.is_alive()
offline=wait_until(lambda s:not s['lm_connected'])
rows=[r for r in offline['requests'] if r['first_observed_at']>=start]
assert rows and all(r['status'] in ('interrupted','cancelled','completed') for r in rows)
assert all(slot['decode_tps'] is None for live in offline['live'].values() for slot in live['slots'].values())
assert offline['gpu']
print('PASS daemon stop: tasks terminal, speed hidden, GPU remains available',flush=True)
command('daemon','up');command('server','start','--bind','127.0.0.1','--port','1234')
command('load','qwen/qwen3-1.7b','--context-length','4096','--parallel','1','--gpu','max','--identifier','v2-switched','--ttl','300','-y')
wait_until(lambda s:s['lm_connected'] and all(s['sources'].get(k,{}).get('ok') for k in ('runtime','model')))
time.sleep(1);new_start=time.time();stats=predict('v2-switched',64)
recovered=wait_until(lambda s:any(r['model']=='v2-switched' and r['first_observed_at']>=new_start and r['status']=='completed' for r in s['requests']))
row=next(r for r in recovered['requests'] if r['model']=='v2-switched' and r['first_observed_at']>=new_start)
assert row['predictedTokensCount']==64 and row.get('totalTimeSec') is not None
assert recovered['sources']['runtime']['last_event_at']>=new_start
assert recovered['sources']['model']['last_event_at']>=new_start
assert row['pid'] not in {r['pid'] for r in rows}
print('PASS daemon restart/model switch: new PID and model correctly attributed; fresh logs and total timing',flush=True)
Path('data/lifecycle-v2.json').write_text(json.dumps({'interrupted':rows,'restarted':row,'prediction_stats':stats},indent=2))
