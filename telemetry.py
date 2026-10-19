"""Version-specific llama.cpp log adapter. Stores numeric telemetry, never model IO."""
import copy
import re
import time
import uuid

SLOT = re.compile(r'id\s+(\d+)\s*\|\s*task\s+(-?\d+)')
SPEED = re.compile(r'n_gen\s*=\s*(\d+),\s*tg\s*=\s*([\d.]+)\s*t/s,\s*tg_3s\s*=\s*([\d.]+)')
PREFILL = re.compile(r'prompt eval time\s*=\s*([\d.]+) ms\s*/\s*(\d+) tokens.*?([\d.]+) tokens per second')
DECODE = re.compile(r'(?<!prompt )\beval time\s*=\s*([\d.]+) ms\s*/\s*(\d+) tokens.*?([\d.]+) tokens per second')
FINISH = re.compile(r'stop processing: n_tokens\s*=\s*(\d+), truncated\s*=\s*(\d+)')
TERMINAL = {'completed', 'cancelled', 'interrupted', 'error'}
FRESH_SECONDS = 12


class Tasks:
    def __init__(self, persist):
        self.persist = persist
        self.session = uuid.uuid4().hex[:12]
        self.tasks = {}
        self.slots = {}

    @staticmethod
    def scope(model, data):
        return (model, str(data.get('instanceReference') or ''), str(data.get('pid') or ''))

    def _new(self, scope, slot, task, ts, observed_start, capacity=None):
        slot_key = (*scope, slot)
        old = self.tasks.get(self.slots.get(slot_key))
        if old and old['status'] not in TERMINAL:
            self._end(old, 'interrupted', ts, 'slot 被新任务复用')
        key = ':'.join((self.session, *scope, slot, task, str(int(ts * 1000))))
        row = dict(request_key=key, kind='runtime', model=scope[0], instance_reference=scope[1],
                   pid=int(scope[2]) if scope[2].isdigit() else None, slot=slot, task=task,
                   started_at=ts if observed_start else None, first_observed_at=ts,
                   updated_at=ts, status='prefill' if observed_start else 'observed',
                   stage_events=[{'phase': 'prefill' if observed_start else 'observed', 'ts': ts}],
                   source='runtime', stopReason=None, context_capacity=capacity)
        self.tasks[key] = row
        self.slots[slot_key] = key
        # Retain only recent finished rows in RAM; full records live in SQLite.
        if len(self.tasks) > 300:
            finished = sorted((r for r in self.tasks.values() if r['status'] in TERMINAL), key=lambda r:r['updated_at'])
            for r in finished[:len(self.tasks)-300]:
                self.tasks.pop(r['request_key'], None)
                stale = (*self.scope(r['model'], {'instanceReference':r['instance_reference'], 'pid':r['pid']}),r['slot'])
                if self.slots.get(stale) == r['request_key']:
                    self.slots.pop(stale, None)
        self.persist(row)
        return row

    def _phase(self, row, phase, ts):
        if row['status'] != phase and row['status'] not in TERMINAL:
            row['status'] = phase
            row['stage_events'].append({'phase':phase, 'ts':ts})

    def _end(self, row, phase, ts, reason=None):
        if row['status'] in TERMINAL:
            return
        self._phase(row, phase, ts)
        row.update(ended_at=ts, updated_at=ts, decode_tps=None)
        if reason:
            row['stopReason'] = reason
        self.persist(row)

    def interrupt(self, reason, model=None):
        now = time.time()
        for row in list(self.tasks.values()):
            if (model is None or row['model'] == model) and row['status'] not in TERMINAL:
                self._end(row, 'interrupted', now, reason)

    def process(self, model, data, ts):
        scope = self.scope(model, data)
        slot = task = None
        for line in str(data.get('message', '')).splitlines():
            # Cancellation has no slot; only cancel the matching process/instance and task.
            cancel = re.search(r'cancel task,\s*id_task\s*=\s*(\d+)', line)
            if cancel:
                for row in list(self.tasks.values()):
                    if (row['model'], row['instance_reference'], str(row['pid'] or '')) == scope and row['task'] == cancel[1]:
                        self._end(row, 'cancelled', ts, '运行时取消')
            match = SLOT.search(line)
            if match:
                slot, task = match.groups()
            # No slot identity means no trustworthy task attribution.
            if slot is None or task is None or task == '-1':
                continue
            speed, prefill, decode, finish = SPEED.search(line), PREFILL.search(line), DECODE.search(line), FINISH.search(line)
            launch = 'processing task' in line and not finish
            progress = re.search(r'prompt processing progress.*?progress\s*=\s*([\d.]+)', line)
            error = data.get('level') == 'error'
            if not any((launch, speed, prefill, decode, finish, progress, error)):
                continue
            row = self.tasks.get(self.slots.get((*scope,slot)))
            if not row or row['task'] != task:
                prior = next((r for r in reversed(list(self.tasks.values()))
                              if (r['model'],r['instance_reference'],str(r['pid'] or ''),r['slot'],r['task']) == (*scope,slot,task)),None)
                if prior:
                    row = prior
                elif row and ts < row['updated_at']:
                    continue
                else:
                    row = self._new(scope,slot,task,ts,launch,data.get('_context_capacity'))
            # Delayed packets must not revive completed or cancelled tasks.
            if ts < row['updated_at']:
                continue
            if row['status'] in TERMINAL:
                if prefill:
                    row.update(prefill_ms=float(prefill[1]),prefill_tokens=int(prefill[2]),prefill_tps=float(prefill[3]))
                if decode:
                    row.update(decode_ms=float(decode[1]),predictedTokensCount=int(decode[2]),tokensPerSecond=float(decode[3]))
                if 'prefill_ms' in row and 'decode_ms' in row:
                    row['totalTimeSec'] = (row['prefill_ms']+row['decode_ms'])/1000
                if finish:
                    row.update(context_tokens=int(finish[1]), truncated=bool(int(finish[2])))
                if prefill or decode or finish:
                    self.persist(row)
                continue
            row['updated_at'] = ts
            if speed:
                self._phase(row,'decode',ts)
                row.update(generated_tokens=int(speed[1]), average_tps=float(speed[2]), decode_tps=float(speed[3]), speed_at=ts)
            if progress:
                row['prefill_progress'] = max(0,min(1,float(progress[1])))
            if prefill:
                row.update(prefill_ms=float(prefill[1]), prefill_tokens=int(prefill[2]), prefill_tps=float(prefill[3]))
            if decode:
                row.update(decode_ms=float(decode[1]), predictedTokensCount=int(decode[2]), tokensPerSecond=float(decode[3]))
            if 'prefill_ms' in row and 'decode_ms' in row:
                row['totalTimeSec'] = (row['prefill_ms']+row['decode_ms'])/1000
            if finish:
                row.update(context_tokens=int(finish[1]), truncated=bool(int(finish[2])))
                self._end(row,'completed',ts)
            elif error:
                self._end(row,'error',ts,'运行时错误（原文未保存）')
            else:
                self.persist(row)

    def live(self, connected, runtime_status, now=None):
        now = now or time.time()
        result = {}
        source_ok = runtime_status.get('ok',False)
        for row in self.tasks.values():
            live = result.setdefault(row['model'], {'slots':{},'active':False})
            key = f"{row['pid']}:{row['instance_reference']}:{row['slot']}"
            prev = live['slots'].get(key)
            if prev and prev['updated_at'] > row['updated_at']:
                continue
            value = copy.deepcopy(row)
            value['active'] = connected and source_ok and row['status'] not in TERMINAL
            value['stale'] = now-row.get('speed_at',0)>FRESH_SECONDS
            if not value['active'] or value['stale']:
                value['decode_tps'] = None
            # This estimates from runtime-processed input and observed generated tokens only.
            value['context_estimate'] = (row['prefill_tokens']+row['generated_tokens']) if value['active'] and 'prefill_tokens' in row and 'generated_tokens' in row else None
            live['slots'][key] = value
        for live in result.values():
            live['active'] = any(s['active'] for s in live['slots'].values())
        return result
