"""Opt-in real LM Studio load test. Requires loaded v2-validation, 2 parallel slots."""
import concurrent.futures
import json
from pathlib import Path
import threading
import time
import urllib.request

BASE='http://127.0.0.1:8090'
def state():return json.load(urllib.request.urlopen(BASE+'/api/state',timeout=4))
started=time.time();snapshots=[];running=True

def sample():
    while running:
        snapshots.append(state());time.sleep(.3)

def predict(cancel=False):
    records='\n'.join(f'Record {n}: Local inference monitoring measures throughput, latency, context occupancy, memory pressure and graphics processor utilization.' for n in range(60))
    payload={'model':'v2-validation','input':f'Test {"cancel" if cancel else "complete"}. Read all records.\n{records}\nWrite a detailed technical report of at least 2000 words on this dataset. /no_think','stream':True,'max_output_tokens':4096 if cancel else 512,'reasoning':'off'}
    req=urllib.request.Request('http://127.0.0.1:1234/api/v1/chat',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    count=0
    with urllib.request.urlopen(req,timeout=90) as response:
        for line in response:
            if not line.startswith(b'data:'):continue
            e=json.loads(line[5:])
            if e.get('type')=='message.delta':
                count+=1
                if cancel and count>=100:return 'client closed'
            if e.get('type')=='chat.end':return e['result']['stats']
    raise RuntimeError('stream ended without chat.end')

thread=threading.Thread(target=sample);thread.start()
try:
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        completed=pool.submit(predict,False);cancelled=pool.submit(predict,True)
        result=[completed.result(),cancelled.result()]
    for _ in range(20):
        final=state();rows=[r for r in final['requests'] if r['model']=='v2-validation' and r['first_observed_at']>=started]
        if len(rows)>=2 and all(r['status'] in ('completed','cancelled','interrupted','error') for r in rows):break
        time.sleep(.5)
finally:
    running=False;thread.join()
rows=[r for r in final['requests'] if r['model']=='v2-validation' and r['first_observed_at']>=started]
print('request_results',json.dumps(result),flush=True)
print('runtime_tasks',json.dumps(rows),flush=True)
assert len(rows)==2,rows
assert len({(r['slot'],r['task']) for r in rows})==2
assert any(r['status']=='completed' and r.get('predictedTokensCount')==512 for r in rows)
assert any(r['status']=='cancelled' for r in rows),'cancel was not identified'
assert any(sum(slot['active'] for l in s['live'].values() for slot in l['slots'].values())>=2 for s in snapshots),'did not observe concurrent tasks'
assert all(not slot['active'] and slot['decode_tps'] is None for l in final['live'].values() for slot in l['slots'].values())
assert any(r['ts']>=started and r['model']=='v2-validation' for r in final['summaries'])
path=Path(__file__).resolve().parents[1]/'data'/'integration-v2.json';path.write_text(json.dumps({'started':started,'results':result,'runtime_tasks':rows,'snapshots':len(snapshots)},indent=2))
print('PASS real concurrent completion, cancellation, slot isolation, speed reset and summary separation')
