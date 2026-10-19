"""Three bounded, sequential local requests for watching telemetry."""
import json
from pathlib import Path
import time
import urllib.request

out=Path(__file__).resolve().parent/'data'/'long-context-results.json'
results=[]
base='http://127.0.0.1:1234'
# Deliberately varied text prefixes prevent all rounds sharing the same prefix cache.
for index,count in enumerate([100,150,200],1):
    records='\n'.join(f'Record {n}: The monitoring agent measures model throughput, memory consumption, processor utilization, request latency, and context capacity during local inference.' for n in range(1,count+1))
    prompt=f'''Observation run {index}. Read all {count} records below.\n{records}\n\nWrite an extensive technical report in English about this monitoring dataset. Discuss each metric, explain prefill and decode, analyze memory pressure, describe troubleshooting, and propose practical improvements. Use detailed paragraphs and aim for at least 2500 words. Do not abbreviate the report or replace sections with ellipses. /no_think'''
    payload={'model':'qwen/qwen3-1.7b','input':prompt,'stream':True,'max_output_tokens':1536,'reasoning':'off'}
    print(f'ROUND {index}/3 START records={count} max_output_tokens=1536',flush=True)
    started=time.monotonic()
    req=urllib.request.Request(base+'/api/v1/chat',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=180) as response:
            for line in response:
                if not line.startswith(b'data:'):continue
                event=json.loads(line[5:])
                kind=event.get('type')
                if kind in ('prompt_processing.start','prompt_processing.end','message.start'):
                    print(f'ROUND {index}: {kind} elapsed={time.monotonic()-started:.1f}s',flush=True)
                if kind=='error':raise RuntimeError(json.dumps(event))
                if kind=='chat.end':
                    stats=event.get('result',{}).get('stats',{})
                    results.append({'round':index,'records':count,'elapsed_seconds':round(time.monotonic()-started,2),'stats':stats})
                    print(f'ROUND {index} DONE '+json.dumps(stats),flush=True)
        if len(results)!=index:raise RuntimeError('Missing chat.end event')
    except Exception as exc:
        failure={'round':index,'error':str(exc)}
        if hasattr(exc,'read'):failure['detail']=exc.read().decode()[:1000]
        results.append(failure);print('FAILED '+json.dumps(failure),flush=True)
    out.write_text(json.dumps(results,indent=2))
print('ALL ROUNDS FINISHED '+str(out),flush=True)
