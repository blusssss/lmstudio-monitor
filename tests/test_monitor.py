import json
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from server import Monitor, Store, number
from telemetry import Tasks

FIX=Path(__file__).parent/'fixtures'
class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.sqlite3'
        self.store=Store(self.path)
        self.monitor=Monitor(types.SimpleNamespace(),self.store)
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def event(self,message,*,task_time=None,model='m',pid=10,ref='instance',level='debug'):
        return {'timestamp':(task_time or time.time())*1000,'data':{'type':'runtime.log','modelIdentifier':model,'instanceReference':ref,'pid':pid,'level':level,'message':message}}
    def feed(self,message,**kwargs):self.monitor.process_event('runtime',self.event(message,**kwargs))
    def test_actual_runtime_trace_has_live_speed_then_finished_context(self):
        events=json.loads((FIX/'runtime.json').read_text())
        for e in events[:4]:self.monitor.process_event('runtime',e)
        row=self.store.requests('runtime')[0]
        self.assertEqual(row['status'],'decode');self.assertEqual(row['decode_tps'],54.4)
        for e in events[4:]:self.monitor.process_event('runtime',e)
        row=self.store.requests('runtime')[0]
        self.assertEqual(row['status'],'completed');self.assertEqual(row['context_tokens'],433)
        self.assertEqual(row['prefill_tokens'],47);self.assertEqual(row['prefill_tps'],882.61)
        self.assertEqual(row['predictedTokensCount'],384);self.assertEqual(row['decode_ms'],7018.10)
        self.assertEqual(len(self.store.requests('runtime')),1)
    def test_output_not_persisted_and_history_survives_restart(self):
        e=json.loads((FIX/'model.json').read_text());e['timestamp']=time.time()*1000;e['data']['output']='DO_NOT_STORE_PROMPT_OR_ANSWER'
        self.monitor.process_event('model',e)
        self.assertNotIn('DO_NOT_STORE',json.dumps(self.monitor.snapshot()))
        other=Store(self.path)
        self.assertEqual(other.requests('summary')[0]['predictedTokensCount'],48)
        self.assertNotIn('output',other.requests()[0]);other.db.close()
    def test_duplicate_summary_is_idempotent(self):
        e=json.loads((FIX/'model.json').read_text())
        self.monitor.process_event('model',e);self.monitor.process_event('model',e)
        self.assertEqual(len(self.store.requests('summary')),1)
    def test_multiline_slot_and_new_task_do_not_reuse_speed(self):
        self.feed('id 2 | task 7 | n_gen = 100, tg = 20.0 t/s, tg_3s = 19.0 t/s\nstop processing: n_tokens = 120, truncated = 0')
        row=self.store.requests('runtime')[0];self.assertEqual(row['slot'],'2');self.assertEqual(row['status'],'completed')
        self.feed('id 2 | task 8 | processing task, is_child = 0')
        row=self.store.requests('runtime')[0];self.assertEqual(row['status'],'prefill');self.assertNotIn('decode_tps',row)
        self.assertEqual(len(self.store.requests('runtime')),2)
    def test_concurrent_same_task_numbers_in_different_processes(self):
        for pid in (10,20):self.feed('id 0 | task 7 | n_gen = 100, tg = 20.0 t/s, tg_3s = 19.0 t/s',pid=pid)
        self.feed('cancel task, id_task = 7',pid=10)
        rows={r['pid']:r for r in self.store.requests('runtime')}
        self.assertEqual(rows[10]['status'],'cancelled');self.assertEqual(rows[20]['status'],'decode')
    def test_cancelled_task_is_not_revived_by_finish_or_late_speed(self):
        now=time.time()
        self.feed('id 0 | task 1 | processing task',task_time=now)
        self.feed('cancel task, id_task = 1',task_time=now+1)
        self.feed('id 0 | task 1 | stop processing: n_tokens = 120, truncated = 0',task_time=now+2)
        self.feed('id 0 | task 1 | n_gen = 100, tg = 20.0 t/s, tg_3s = 19.0 t/s',task_time=now+.5)
        row=self.store.requests('runtime')[0]
        self.assertEqual(row['status'],'cancelled');self.assertEqual(row['context_tokens'],120);self.assertIsNone(row['decode_tps'])
    def test_runtime_summary_not_guessed_onto_concurrent_tasks(self):
        for slot in (0,1):self.feed(f'id {slot} | task {slot+5} | processing task')
        e=json.loads((FIX/'model.json').read_text());e['data']['modelIdentifier']='m'
        self.monitor.process_event('model',e)
        self.assertEqual(len(self.store.requests('runtime')),2)
        self.assertTrue(all('promptTokensCount' not in r for r in self.store.requests('runtime')))
        self.assertEqual(self.store.requests('summary')[0]['association'],'unlinked')
    def test_speed_hidden_when_stale_offline_or_subscription_down(self):
        now=time.time()
        self.feed('id 0 | task 7 | n_gen = 100, tg = 20.0 t/s, tg_3s = 19.0 t/s',task_time=now)
        def slot(connected,ok,t):return next(iter(self.monitor.tasks.live(connected,{'ok':ok},t)['m']['slots'].values()))
        self.assertEqual(slot(True,True,now+2)['decode_tps'],19)
        for connected,ok,t in [(True,True,now+13),(False,True,now+2),(True,False,now+2)]:self.assertIsNone(slot(connected,ok,t)['decode_tps'])
    def test_disconnect_ends_active_tasks(self):
        self.feed('id 0 | task 7 | processing task')
        self.monitor.disconnected('test disconnect')
        self.assertEqual(self.store.requests('runtime')[0]['status'],'interrupted')
    def test_recover_unfinished_task_and_keep_summary_unchanged(self):
        self.feed('id 0 | task 7 | processing task')
        self.store.request(time.time(),'m',{'kind':'summary','predictedTokensCount':48})
        self.store.recover()
        self.assertEqual(self.store.requests('runtime')[0]['status'],'interrupted')
        self.assertNotIn('status',self.store.requests('summary')[0])
    def test_migrate_v1_database_without_losing_history(self):
        path=Path(self.tmp.name)/'old.sqlite3';db=sqlite3.connect(path)
        db.execute('CREATE TABLE requests(id INTEGER PRIMARY KEY, ts REAL, model TEXT, data TEXT)')
        db.execute('INSERT INTO requests VALUES (1,?,?,?)',(time.time(),'old',json.dumps({'predictedTokensCount':5})));db.commit();db.close()
        old=Store(path);self.assertEqual(old.requests('summary')[0]['predictedTokensCount'],5)
        old.request(time.time(),'new',{'kind':'summary'});self.assertEqual(len(old.requests('summary')),2);old.db.close()
    def test_old_instance_does_not_merge_with_new_instance(self):
        self.feed('id 0 | task 0 | processing task',ref='first')
        self.feed('id 0 | task 0 | processing task',ref='second')
        self.assertEqual(len(self.store.requests('runtime')),2)
    def test_late_old_task_cannot_interrupt_new_slot_owner(self):
        now=time.time()
        self.feed('id 0 | task 1 | processing task',task_time=now)
        self.feed('id 0 | task 2 | processing task',task_time=now+2)
        self.feed('id 0 | task 1 | stop processing: n_tokens = 100, truncated = 0',task_time=now+3)
        rows={r['task']:r for r in self.store.requests('runtime')}
        self.assertEqual(rows['2']['status'],'prefill');self.assertEqual(len(rows),2)
    def test_prefill_and_decode_metrics_in_separate_events(self):
        self.feed('id 0 | task 1 | prompt eval time = 20.0 ms / 10 tokens (2.0 ms per token, 500.0 tokens per second)')
        self.feed('id 0 | task 1 | eval time = 100.0 ms / 5 tokens (20.0 ms per token, 50.0 tokens per second)')
        row=self.store.requests('runtime')[0];self.assertAlmostEqual(row['totalTimeSec'],.12)
    def test_finish_metrics_after_cancel_enrich_without_reviving(self):
        self.feed('id 0 | task 1 | processing task')
        self.feed('cancel task, id_task = 1')
        self.feed('id 0 | task 1 | eval time = 100.0 ms / 5 tokens (20.0 ms per token, 50.0 tokens per second)')
        row=self.store.requests('runtime')[0];self.assertEqual(row['status'],'cancelled');self.assertEqual(row['predictedTokensCount'],5)
    def test_capacity_is_snapshot_of_first_task_observation(self):
        e=self.event('id 0 | task 1 | processing task');e['data']['_context_capacity']=4096
        self.monitor.tasks.process('m',e['data'],e['timestamp']/1000)
        e=self.event('id 0 | task 1 | stop processing: n_tokens = 100, truncated = 0');e['data']['_context_capacity']=8192
        self.monitor.tasks.process('m',e['data'],e['timestamp']/1000)
        self.assertEqual(self.store.requests('runtime')[0]['context_capacity'],4096)
    def test_clear_history_keeps_active_tasks_and_removes_finished_models(self):
        for e in json.loads((FIX/'runtime.json').read_text()):self.monitor.process_event('runtime',e)
        self.monitor.process_event('model',json.loads((FIX/'model.json').read_text()))
        self.feed('id 0 | task 88 | processing task',model='active-model')
        self.store.sample({'ts':time.time(),'decode_tps':20})
        result=self.monitor.clear_history()
        state=self.monitor.snapshot()
        self.assertEqual(result['active_preserved'],1);self.assertEqual(result['requests_deleted'],2)
        self.assertEqual(state['summaries'],[]);self.assertEqual(self.store.history(86400),[])
        self.assertEqual(set(state['live']),{'active-model'})
        self.assertEqual(state['requests'][0]['status'],'prefill')
        # Windows wall-clock readings can be equal across these immediate calls.
        self.feed('id 0 | task 88 | stop processing: n_tokens = 100, truncated = 0',model='active-model',task_time=result['history_revision']+.001)
        self.assertEqual(self.store.requests('runtime')[0]['status'],'completed')
    def test_clear_revision_survives_restart_and_blocks_stale_history(self):
        old=self.event('id 0 | task 1 | processing task')
        self.monitor.process_event('runtime',old)
        self.feed('id 0 | task 1 | stop processing: n_tokens = 10, truncated = 0')
        previous_revision=self.store.history_revision
        result=self.monitor.clear_history()
        self.monitor.process_event('runtime',old)
        self.assertEqual(self.store.requests(),[])
        self.store.sample({'ts':time.time(),'history_revision':previous_revision,'decode_tps':99})
        self.assertEqual(self.store.history(86400),[])
        other=Store(self.path);self.assertEqual(other.history_revision,result['history_revision']);other.db.close()
    def test_clear_http_requires_json_and_same_origin(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        from server import Handler
        import threading
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.monitor=self.monitor
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.store.request(time.time(),'old',{'kind':'summary'})
        def post(headers,body='{}'):
            conn=HTTPConnection('127.0.0.1',http.server_port,timeout=3)
            conn.request('POST','/api/history/clear',body,headers)
            response=conn.getresponse();data=response.read();status=response.status;conn.close();return status,data
        try:
            status,_=post({'Content-Type':'application/json','Origin':'https://external.example'})
            self.assertEqual(status,403);self.assertEqual(len(self.store.requests()),1)
            self.assertEqual(post({'Content-Type':'text/plain'})[0],415)
            self.assertEqual(post({'Content-Type':'application/json'},'bad')[0],400)
            status,body=post({'Content-Type':'application/json'})
            self.assertEqual(status,200);self.assertEqual(json.loads(body)['requests_deleted'],1)
            self.assertEqual(self.store.requests(),[])
        finally:http.shutdown();http.server_close();thread.join()
    def test_unscoped_lines_do_not_invent_a_slot(self):
        self.feed('eval time = 10.0 ms / 5 tokens (2.0 ms per token, 500.0 tokens per second)')
        self.assertEqual(self.store.requests('runtime'),[])
    def test_historical_retention_and_sampling_interval(self):
        now=time.time()
        self.store.sample({'ts':now-90000,'decode_tps':20});self.store.sample({'ts':now,'decode_tps':40});self.store.sample({'ts':now+1,'decode_tps':50})
        self.assertEqual(len(self.store.history(86400)),1);self.assertEqual(self.store.history(86400)[0]['decode_tps'],40)
    def test_unavailable_gpu_values_are_not_zero(self):
        for x in ['N/A','[Not Supported]',None,'nan','inf']:self.assertIsNone(number(x))
    def test_reused_slot_closes_unfinished_previous_task(self):
        self.feed('id 0 | task 1 | processing task');self.feed('id 0 | task 2 | processing task')
        rows={r['task']:r for r in self.store.requests('runtime')}
        self.assertEqual(rows['1']['status'],'interrupted');self.assertEqual(rows['2']['status'],'prefill')

if __name__=='__main__':unittest.main()
