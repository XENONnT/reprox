"""Failure-injection coverage. Scheduler calls never submit real jobs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from reprox import online_oom_retry as retry


class FakeOp:
    FAILED='failed'; SUBMITTING='submitting'; SUBMITTED='submitted'
    now=pd.Timestamp('2026-10-07 06:00:00')
    def __init__(self, root):self.root=root
    @staticmethod
    def utc_now():return FakeOp.now
    @contextmanager
    def state_lock(self,path):
        import fcntl
        with open(path+'.lock','a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(stream,fcntl.LOCK_UN)
    @staticmethod
    def load_state(path):return pd.read_pickle(path)
    @staticmethod
    def save_state(path,frame):frame.to_pickle(path)
    def read_log(self,n):
        path=self.root/f'{n:06d}.txt'
        return path.read_text() if path.exists() else '',str(path)
    @staticmethod
    def extract_job_id(value):return str(value) if str(value).isdecimal() else ''


class FakeBackend:
    def __init__(self,root):
        self.root=root;self.pd=pd;self.op=FakeOp(root);self.state_file=root/'state.h5'
        self.max_jobs=20;self.partition='lgrandi';self.queue_rows=[];self.records={}
        self.missing=[];self.permitted=True;self.fail_submit=False;self.after_accept=False
        self.submit_count=0;self.jobs=[];self.hide_new_jobs=False
    def queue(self):return list(self.queue_rows)
    def accounting(self,jobs):return {k:v for k,v in self.records.items() if k.split('.')[0] in jobs}
    def allowed(self,row):return self.permitted
    def check_inputs(self,n):return self.missing
    def build_job(self,n,targets,memory):
        backend=self
        class Job:
            submit_kwargs={'mem_per_cpu':memory,'cpus_per_task':1,'container':'existing.simg','jobstring':'original straxer command'}
            submit_message=None
            def submit(self,**kwargs):
                backend.submit_count+=1
                backend.jobs.append(self)
                if backend.fail_submit:raise RuntimeError('scheduler unavailable')
                self.submit_message=str(9000+backend.submit_count)
                if not backend.hide_new_jobs:backend.queue_rows.append([self.submit_message,f'{n:06d}-event_reprocess_oom','PENDING'])
                backend.records[self.submit_message]={'name':f'{n:06d}-event_reprocess_oom','state':'OUT_OF_MEMORY','memory':f'{memory}Mc','cpus':1}
                if backend.after_accept:raise RuntimeError('reply lost after acceptance')
        return Job()
    def add_run(self,n=123,job='456',status='failed',memory='18000Mc',state='OUT_OF_MEMORY'):
        row=dict(status=status,job_id=job,attempts=1,progress=37.8,targets='event_info',
                 source='rn-220',mode='tpc_radon',submitted_at=pd.Timestamp('2026-10-07 05:00'),updated_at=FakeOp.now-pd.Timedelta(seconds=120))
        frame=self.op.load_state(self.state_file) if self.state_file.exists() else pd.DataFrame()
        frame=pd.concat([frame,pd.DataFrame([row],index=[n])]).sort_index()
        self.op.save_state(self.state_file,frame)
        self.records[job]={'name':f'{n:06d}-event_reprocess','state':state,'memory':memory,'cpus':1}
        (self.root/f'{n:06d}.txt').write_text('slurmstepd: oom-kill\n')


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.backend=FakeBackend(self.root);self.backend.add_run()
        self.ledger=self.root/'ledger.json';self.audit=self.root/'audits';self.policy=retry.Policy()
    def cycle(self,submit=False,max_submit=1):
        return retry.run_cycle(self.backend,self.policy,self.ledger,self.audit,submit,max_submit)
    def frame(self):return self.backend.op.load_state(self.backend.state_file)
    def test_memory_tiers_strictly_increase(self):
        for old,want in [(18000,32768),(22426,32768),(32768,49152),(48000,49152),(49152,None),(64000,None)]:
            with self.subTest(old=old):self.assertEqual(self.policy.next_memory(old,0),want)
        self.assertIsNone(self.policy.next_memory(18000,2))
    def test_reqmem_units_and_invalid_values(self):
        for raw,want in [('18000Mc',18000),('32Gc',32768),('32Gn',32768),('22426M',22426),('1024Kc',1)]:
            with self.subTest(raw=raw):self.assertEqual(retry.memory_mib(raw),want)
        for raw in ['', 'unknown', '-1M', '0Mc']:
            with self.subTest(raw=raw),self.assertRaises(ValueError):retry.memory_mib(raw)
    def test_only_terminal_scheduler_oom_qualifies(self):
        for state in ['RUNNING','COMPLETING','PENDING','COMPLETED','FAILED','CANCELLED','TIMEOUT','NODE_FAIL']:
            with self.subTest(state=state):
                self.backend.records['456']['state']=state
                self.assertEqual(self.cycle()[0]['action'],'not_confirmed_oom')
        self.assertEqual(self.backend.submit_count,0)
    def test_failed_parent_with_oom_step_is_confirmed(self):
        self.backend.records['456']['state']='FAILED'
        self.backend.records['456.batch']={'name':'batch','state':'OUT_OF_MEMORY','memory':'18000Mc','cpus':1}
        self.assertEqual(self.cycle()[0]['action'],'would_submit')
    def test_accounting_parser_does_not_truncate_oom(self):
        records=retry.accounting_rows('456|000123-event_reprocess|OUT_OF_MEMORY+|18000Mc|1\n456.batch|batch|OUT_OF_MEMORY|18000Mc|1\n')
        self.assertTrue(retry.confirmed_oom(records,'456',123))
        with self.assertRaises(retry.UnsafeRetry):retry.accounting_rows('not an accounting record')
    def test_unknown_job_or_wrong_run_or_multiple_cpus_is_ignored(self):
        self.backend.records={};self.assertEqual(self.cycle()[0]['action'],'not_confirmed_oom')
        self.backend.records={'456':{'name':'000999-event_reprocess','state':'OUT_OF_MEMORY','memory':'18000Mc','cpus':1}}
        self.assertEqual(self.cycle()[0]['action'],'not_confirmed_oom')
        self.backend.records['456'].update(name='000123-event_reprocess',cpus=2)
        self.assertEqual(self.cycle()[0]['action'],'not_confirmed_oom')
    def test_preview_has_no_state_or_log_or_ledger_changes(self):
        before=self.backend.state_file.read_bytes();log=(self.root/'000123.txt').read_bytes()
        self.assertEqual(self.cycle()[0]['memory_mib'],32768)
        self.assertEqual(self.backend.state_file.read_bytes(),before)
        self.assertEqual((self.root/'000123.txt').read_bytes(),log)
        self.assertFalse(self.ledger.exists());self.assertFalse(self.audit.exists())
    def test_running_duplicate_blocks_retry_even_if_old_job_is_terminal(self):
        self.backend.queue_rows=[['789','000123-event_reprocess','RUNNING']]
        self.assertEqual(self.cycle(True)[0]['action'],'active_job');self.assertEqual(self.backend.submit_count,0)
    def test_full_queue_or_missing_inputs_do_not_mutate_state(self):
        self.backend.max_jobs=1;self.backend.queue_rows=[['789','000999-event_reprocess','PENDING']]
        self.assertEqual(self.cycle(True)[0]['action'],'queue_full')
        self.backend.queue_rows=[];self.backend.missing=['peaklets']
        self.assertEqual(self.cycle(True)[0]['action'],'waiting_for_input')
        self.assertFalse(self.ledger.exists());self.assertEqual(self.frame().at[123,'attempts'],1)
    def test_cooldown_and_excluded_sources(self):
        frame=self.frame();frame.at[123,'updated_at']=FakeOp.now;self.backend.op.save_state(self.backend.state_file,frame)
        self.assertEqual(self.cycle(True)[0]['action'],'cooldown')
        self.backend.permitted=False;self.assertEqual(self.cycle(True)[0]['action'],'excluded_source')
    def test_success_archives_log_and_preserves_other_rows(self):
        self.backend.add_run(124,'457',status='moved');before=self.frame().loc[[124]].copy()
        result=self.cycle(True)[0];self.assertEqual(result['job_id'],'9001')
        self.assertEqual(self.frame().at[123,'status'],'submitted');self.assertEqual(self.frame().at[123,'attempts'],2)
        pd.testing.assert_frame_equal(before,self.frame().loc[[124]])
        history=json.loads(self.ledger.read_text());entry=history['runs']['123'][0]
        self.assertEqual(Path(entry['archived_log']).read_text(),'slurmstepd: oom-kill\n')
        self.assertTrue(Path(entry['audit_dir'],'accepted.json').is_file())
        self.assertTrue(Path(entry['audit_dir'],'receipt.json').is_file())
        self.assertEqual(Path(entry['audit_dir'],'before.h5').stat().st_mode & 0o222,0)
        self.assertEqual(self.backend.jobs[0].submit_kwargs['mem_per_cpu'],32768)
    def test_two_watchers_and_restored_old_h5_cannot_duplicate(self):
        before=self.frame();self.cycle(True)
        self.backend.queue_rows=[];self.backend.op.save_state(self.backend.state_file,before)
        self.assertEqual(self.cycle(True)[0]['action'],'already_journaled')
        self.assertEqual(self.backend.submit_count,1)
    def test_second_oom_escalates_to_48gib_and_then_stops(self):
        self.cycle(True);self.backend.queue_rows=[]
        frame=self.frame();frame.at[123,'status']='failed';frame.at[123,'updated_at']=FakeOp.now-pd.Timedelta(seconds=120)
        self.backend.op.save_state(self.backend.state_file,frame)
        self.assertEqual(self.cycle(True)[0]['memory_mib'],49152)
        self.backend.queue_rows=[];frame=self.frame();frame.at[123,'status']='failed';frame.at[123,'updated_at']=FakeOp.now-pd.Timedelta(seconds=120)
        self.backend.op.save_state(self.backend.state_file,frame)
        self.assertEqual(self.cycle(True)[0]['action'],'memory_or_retry_limit')
        self.assertEqual(self.backend.submit_count,2)
    def test_cycle_limit_and_slurm_visibility_lag(self):
        self.backend.add_run(124,'457');result=self.cycle(True)
        self.assertEqual([r['action'] for r in result],['submitted','cycle_limit'])
        # Only one slot, with a newly accepted job not visible in squeue yet.
        self.backend.max_jobs=1;self.backend.queue_rows=[];self.backend.hide_new_jobs=True
        self.backend.add_run(125,'458');result=self.cycle(True,max_submit=3)
        self.assertEqual([r['action'] for r in result],['submitted','queue_full'])
    def test_submit_exception_retains_uncertain_state_and_blocks_restart(self):
        self.backend.after_accept=True
        with self.assertRaises(retry.UnsafeRetry):self.cycle(True)
        self.assertEqual(self.frame().at[123,'status'],'submitting')
        self.assertEqual(self.frame().at[123,'attempts'],2)
        entry=json.loads(self.ledger.read_text())['runs']['123'][0]
        self.assertEqual(entry['status'],'uncertain')
        with self.assertRaises(retry.UnsafeRetry):self.cycle(True)
        self.assertEqual(self.backend.submit_count,1)
    def test_h5_failure_after_acceptance_preserves_job_receipt(self):
        original=retry.publish_one_row;calls=[]
        def publish(*args,**kwargs):
            calls.append(True)
            if len(calls)==2:raise OSError('injected H5 publication failure')
            return original(*args,**kwargs)
        with patch.object(retry,'publish_one_row',publish),self.assertRaises(retry.UnsafeRetry):self.cycle(True)
        entry=json.loads(self.ledger.read_text())['runs']['123'][0]
        self.assertEqual(entry['job_id'],'9001')
        self.assertTrue(Path(entry['audit_dir'],'accepted.json').is_file())
        self.assertEqual(self.frame().at[123,'status'],'submitting')
        self.assertEqual(self.backend.submit_count,1)
    def test_missing_or_corrupt_ledger_does_not_reset_limits(self):
        self.cycle(True);self.ledger.unlink()
        with self.assertRaises(retry.UnsafeRetry):self.cycle(True)
        self.ledger.write_text('not JSON')
        with self.assertRaises(json.JSONDecodeError):self.cycle(True)
    def test_scheduler_failure_leaves_state_unchanged(self):
        before=self.backend.state_file.read_bytes()
        with patch.object(self.backend,'accounting',side_effect=retry.UnsafeRetry('sacct unavailable')),self.assertRaises(retry.UnsafeRetry):self.cycle(True)
        self.assertEqual(self.backend.state_file.read_bytes(),before);self.assertEqual(self.backend.submit_count,0)
    def test_missing_state_is_not_restored_or_recreated(self):
        self.backend.state_file.unlink()
        with self.assertRaises(retry.UnsafeRetry):self.cycle(True)
        self.assertFalse(self.backend.state_file.exists())

    def test_backend_rejects_config_loaded_before_command_line(self):
        from reprox import core
        config=self.root/'other.ini';config.write_text('[context]\n')
        with patch.dict(os.environ),self.assertRaisesRegex(retry.UnsafeRetry,'REPROX_CONFIG'):
            retry.ReproxBackend(Path(core.reprox_dir).parent,config)

    def test_backend_rejects_different_imported_checkout(self):
        from reprox import core
        with patch.dict(os.environ),self.assertRaisesRegex(retry.UnsafeRetry,'checkout'):
            retry.ReproxBackend(self.root,Path(core.config_path))

    def test_actual_reprox_hdf_publication_and_full_submission_transaction(self):
        from reprox import core,online_processing as op
        old_log=core.log_fn
        self.addCleanup(setattr,core,'log_fn',old_log)
        core.log_fn=str(self.root/'{run_id}.txt')
        frame=op.discover_runs(op.empty_state(),[{'number':123,'mode':'tpc_radon','source':'rn-220'},
            {'number':124,'mode':'tpc_radon','source':'rn-220'}])
        frame.at[123,'status']=op.FAILED;frame.at[123,'job_id']='456';frame.at[123,'attempts']=1
        frame.at[123,'updated_at']=op.utc_now()-pd.Timedelta(seconds=120)
        frame.at[124,'status']=op.MOVED
        op.save_state(str(self.backend.state_file),frame)
        before=op.load_state(str(self.backend.state_file)).loc[[124]].copy()
        self.backend.op=op
        self.assertEqual(self.cycle(True)[0]['job_id'],'9001')
        after=op.load_state(str(self.backend.state_file));self.assertEqual(after.at[123,'status'],op.SUBMITTED)
        pd.testing.assert_frame_equal(before,after.loc[[124]])
        ledger=json.loads(self.ledger.read_text());backup=Path(ledger['runs']['123'][0]['audit_dir'])/'before.h5'
        self.assertEqual(op.load_state(str(backup)).at[123,'status'],op.FAILED)


if __name__=='__main__':unittest.main()
