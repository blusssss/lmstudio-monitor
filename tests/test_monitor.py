import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from server import Monitor, Store, number

FIX=Path(__file__).parent/'fixtures'
class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.sqlite3')
        self.monitor=Monitor(types.SimpleNamespace(),self.store)
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def test_actual_runtime_trace_has_live_speed_then_finished_context(self):
        events=json.loads((FIX/'runtime.json').read_text())
        for e in events[:4]:self.monitor.process_event('runtime',e)
        slot=self.monitor.snapshot()['live']['monitor-validation']['slots']['0']
        self.assertTrue(slot['active']);self.assertEqual(slot['decode_tps'],54.4)
        for e in events[4:]:self.monitor.process_event('runtime',e)
        slot=self.monitor.snapshot()['live']['monitor-validation']['slots']['0']
        self.assertFalse(slot['active']);self.assertEqual(slot['context_tokens'],433)
        self.assertEqual(slot['prefill_tokens'],47);self.assertEqual(slot['prefill_tps'],882.61)
    def test_output_not_persisted_and_history_survives_restart(self):
        e=json.loads((FIX/'model.json').read_text());e['timestamp']=time.time()*1000;e['data']['output']='DO_NOT_STORE_PROMPT_OR_ANSWER'
        self.monitor.process_event('model',e)
        self.assertNotIn('DO_NOT_STORE',json.dumps(self.monitor.snapshot()))
        other=Store(Path(self.tmp.name)/'test.sqlite3')
        self.assertEqual(other.requests()[0]['predictedTokensCount'],48)
        self.assertNotIn('output',other.requests()[0]);other.db.close()
    def test_split_multiline_slot_and_new_task_do_not_reuse_speed(self):
        def event(message):return {'timestamp':time.time()*1000,'data':{'modelIdentifier':'m','message':message}}
        self.monitor.process_event('runtime',event('id  2 | task 7 | n_gen = 100, tg = 20.0 t/s, tg_3s = 19.0 t/s\nstop processing: n_tokens = 120, truncated = 0'))
        slots=self.monitor.snapshot()['live']['m']['slots'];self.assertFalse(slots['2']['active']);self.assertNotIn('0',slots)
        self.monitor.process_event('runtime',event('id  2 | task 8 | processing task, is_child = 0'))
        self.assertNotIn('decode_tps',self.monitor.snapshot()['live']['m']['slots']['2'])
    def test_historical_retention_and_sampling_interval(self):
        now=time.time()
        self.store.sample({'ts':now-90000,'decode_tps':20})
        self.store.sample({'ts':now,'decode_tps':40})
        self.store.sample({'ts':now+1,'decode_tps':50})
        self.assertEqual(len(self.store.history(86400)),1)
        self.assertEqual(self.store.history(86400)[0]['decode_tps'],40)
    def test_unavailable_gpu_values_are_not_zero(self):
        for x in ['N/A','[Not Supported]',None,'nan','inf']:
            self.assertIsNone(number(x))

if __name__=='__main__':unittest.main()
