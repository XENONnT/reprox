#!/usr/bin/env python3
"""Retry confirmed Slurm OOM failures through the existing reprox submission chain.

Default is preview only. --submit enables bounded retries; existing processing
and validation listeners track accepted jobs. Export REPROX_CONFIG before
importing reprox or using the module/command entry point.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

LOG = logging.getLogger('oom_retry')


class UnsafeRetry(RuntimeError):
    """Stop rather than risk an uncertain or duplicate submission."""


@dataclass(frozen=True)
class Policy:
    memory_steps: tuple = (32768, 49152)
    max_retries: int = 2
    cooldown_seconds: int = 60

    def __post_init__(self):
        if not self.memory_steps or tuple(sorted(set(self.memory_steps))) != self.memory_steps:
            raise ValueError('Memory steps must be positive, strictly increasing MiB values')
        if self.memory_steps[0] <= 0 or self.max_retries <= 0 or self.cooldown_seconds < 0:
            raise ValueError('Invalid retry limit or cooldown')

    def next_memory(self, previous_mib, count):
        if count >= self.max_retries:
            return None
        return next((m for m in self.memory_steps if m > previous_mib), None)


def memory_mib(value):
    """Parse Slurm ReqMem. This listener supports one-CPU source jobs only."""
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([KMGT])([cn]?)', value.strip(), re.I)
    if not match:
        raise ValueError(f'Unknown Slurm ReqMem: {value!r}')
    number, unit, _scope = match.groups()
    result = math.ceil(float(number) * {'K':1/1024, 'M':1, 'G':1024, 'T':1024**2}[unit.upper()])
    if result <= 0:
        raise ValueError('Slurm memory must be positive')
    return result


def accounting_rows(text):
    """JobIDRaw|JobName|State|ReqMem|AllocCPUS, including batch steps."""
    rows = {}
    for line in text.splitlines():
        values = line.strip().split('|')
        if values and not values[-1]:
            values.pop()
        if len(values) != 5:
            raise UnsafeRetry(f'Unexpected accounting row: {line!r}')
        job_id, name, state, mem, cpus = values
        rows[job_id] = {'name':name, 'state':state.split()[0].rstrip('+'),
                        'memory':mem, 'cpus':int(cpus or 0)}
    return rows


def confirmed_oom(records, job_id, run):
    parent = records.get(job_id)
    if not parent or not parent['name'].startswith(f'{run:06d}-') or parent['cpus'] != 1:
        return False
    if parent['state'] == 'OUT_OF_MEMORY':
        return True
    # A failed parent with an OOM batch step is also explicit scheduler evidence.
    return parent['state'] == 'FAILED' and any(
        key.startswith(job_id + '.') and row['state'] == 'OUT_OF_MEMORY'
        for key,row in records.items())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        with open(name) as stream:
            assert json.load(stream) == value
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_history(path, state_file, audit_root):
    if Path(path).exists():
        with open(path) as stream:
            history = json.load(stream)
        if history.get('version') != 1 or history.get('state_file') != str(Path(state_file).resolve()):
            raise UnsafeRetry('Retry ledger belongs to a different state file or version')
        if not isinstance(history.get('runs'), dict):
            raise UnsafeRetry('Invalid retry ledger')
    else:
        history = {'version':1, 'state_file':str(Path(state_file).resolve()), 'runs':{}}
    entries = [entry for values in history['runs'].values() for entry in values]
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get('transaction') or not entry.get('failed_job_id'):
            raise UnsafeRetry('Incomplete retry ledger entry')
        if entry.get('status') != 'submitted':
            raise UnsafeRetry(f"Unresolved retry; inspect {entry.get('audit_dir')} and Slurm before continuing")
    known = {entry['transaction'] for entry in entries}
    for plan in Path(audit_root).glob('*/plan.json'):
        if plan.parent.name not in known:
            raise UnsafeRetry(f'Unjournaled retry plan; inspect {plan.parent} before continuing')
    return history


def publish_one_row(op, path, run, desired, allowed_ids):
    """Reuse the manual-retry publication pattern under the canonical state lock."""
    path = Path(path)
    for _ in range(3):
        signature = path.stat()
        current = op.load_state(str(path))
        if str(current.at[run,'job_id']) not in allowed_ids:
            raise UnsafeRetry('Run job ID changed during retry publication')
        expected_status = op.FAILED if desired.at[run,'status'] == op.SUBMITTING else op.SUBMITTING
        if current.at[run,'status'] != expected_status:
            raise UnsafeRetry('Run status changed during retry publication')
        others = current.drop(index=run).copy(deep=True)
        current.loc[run] = desired.loc[run]
        staged = path.with_name(path.name + '.oom-' + uuid.uuid4().hex)
        try:
            op.save_state(str(staged), current)
            latest = path.stat()
            if (signature.st_ino,signature.st_size,signature.st_mtime_ns) != (latest.st_ino,latest.st_size,latest.st_mtime_ns):
                continue
            os.replace(staged, path)
            saved = op.load_state(str(path))
            if not saved.loc[[run]].equals(current.loc[[run]]) or not saved.drop(index=run).equals(others):
                raise UnsafeRetry('State readback differs after publication')
            return saved
        finally:
            if staged.exists():
                staged.unlink()
    raise UnsafeRetry('State changed during publication; inspect before retrying')


class ReproxBackend:
    def __init__(self, repo, config, state_file=None):
        os.environ['REPROX_CONFIG'] = str(Path(config).resolve())
        sys.path.insert(0, str(Path(repo).resolve()))
        import pandas as pd
        from reprox import core, online_processing as op
        # Package imports have already loaded core. Never mix the command-line
        # configuration or checkout with a different imported submission chain.
        if Path(core.config_path).resolve() != Path(config).resolve():
            raise UnsafeRetry('Export REPROX_CONFIG before importing reprox or launching the module')
        if Path(core.reprox_dir).resolve() != (Path(repo).resolve()/'reprox'):
            raise UnsafeRetry('Launch reprox from the checkout supplied by --repo')
        self.pd, self.core, self.op = pd, core, op
        if int(core.config['processing']['cpus_per_job']) != 1:
            raise ValueError('OOM listener currently supports one CPU per job')
        self.state_file = Path(state_file or Path(core.config['context']['base_folder'])/'online_processing.h5').resolve()
        if self.state_file.parent != Path(core.config['context']['base_folder']).resolve():
            raise ValueError('State file must belong to the configured base_folder')
        self.max_jobs = int(core.config['processing']['max_jobs'])
        self.partition = core.config['processing']['allowed_partitions'].split(',')[0].strip()
        self.input_context = None

    @staticmethod
    def command(argv):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise UnsafeRetry(f'{argv[0]} failed ({result.returncode}): {result.stderr.strip()}')
        return result.stdout

    def queue(self):
        text = self.command(['squeue','-h','--user',os.environ['USER'],'-o','%i|%j|%T'])
        rows = []
        for line in text.splitlines():
            parts = line.split('|')
            if len(parts) != 3:
                raise UnsafeRetry('Unexpected squeue output')
            rows.append(parts)
        return rows

    def accounting(self, jobs):
        if not jobs:
            return {}
        text = self.command(['sacct','--user',os.environ['USER'],'-j',','.join(jobs),'-n','-P',
            '-o','JobIDRaw,JobName%80,State%30,ReqMem,AllocCPUS'])
        return accounting_rows(text)

    def allowed(self, row):
        source = str(row['source']).strip().lower()
        excluded = self.op.configured_excluded_sources()
        return not (source in excluded or ('kr-83m' in excluded and 'kr83m' in str(row['mode']).lower()))

    def check_inputs(self, run):
        if self.input_context is None:
            self.input_context = self.op.make_input_context(self.op.rucio_local_path())
        from straxen.storage.rucio_local import rucio_path
        missing = []
        for dtype in self.op.configured_prerequisites():
            run_id = f'{run:06d}'
            if not self.input_context.is_stored(run_id, dtype):
                missing.append(dtype); continue
            key = self.input_context.key_for(run_id, dtype)
            scope = f'xnt_{run_id}'
            root = self.op.rucio_local_path()
            path = Path(rucio_path(root, f'{scope}:{dtype}-{key.lineage_hash}-metadata.json'))
            with path.open() as stream:
                metadata = json.load(stream)
            chunks = [c for c in metadata.get('chunks',[]) if c.get('filename')]
            if not metadata.get('writing_ended') or metadata.get('exception') or not chunks:
                missing.append(dtype); continue
            for chunk in chunks:
                path = Path(rucio_path(root, f"{scope}:{chunk['filename']}"))
                if not path.is_file() or not path.stat().st_size or (
                        isinstance(chunk.get('filesize'),(int,float)) and path.stat().st_size != chunk['filesize']):
                    missing.append(dtype); break
        return missing

    def build_job(self, run, targets, memory):
        job = self.op.build_job(run, targets)
        # Set the final request AFTER reprox's calibration multiplier.
        job.submit_kwargs['mem_per_cpu'] = memory
        job.submit_kwargs['cpus_per_task'] = 1
        job.submit_kwargs['jobname'] += f'_oom{memory}MiB'
        return job


def submit_retry(backend, frame, run, previous_memory, memory, history, ledger, audit_root):
    op = backend.op
    old_job = str(frame.at[run,'job_id'])
    attempt = int(frame.at[run,'attempts']) + 1
    transaction = f'{run:06d}.{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}.{uuid.uuid4().hex[:8]}'
    audit = Path(audit_root)/transaction
    audit.mkdir(parents=True, exist_ok=False)
    _text, old_log = op.read_log(run)
    old_log = Path(old_log)
    archived = str(old_log) + f'.oom-{transaction}.bak' if old_log.exists() else None
    job = backend.build_job(run, str(frame.at[run,'targets']), memory)
    plan = {'transaction':transaction,'run':run,'failed_job_id':old_job,'previous_memory_mib':previous_memory,
            'memory_mib':memory,'cpus':1,'attempts':attempt,'status':'reserved','job_id':'',
            'archived_log':archived,'audit_dir':str(audit),'worker_container':job.submit_kwargs.get('container'),
            'jobstring':job.submit_kwargs.get('jobstring'),'created_at_utc':datetime.now(timezone.utc).isoformat()}
    shutil.copy2(backend.state_file, audit/'before.h5')
    (audit/'before.h5').chmod(0o444)
    op.load_state(str(audit/'before.h5'))
    if old_log.exists():
        shutil.copy2(old_log, audit/'previous_log.txt')
    atomic_json(audit/'plan.json', plan)
    entries = history['runs'].setdefault(str(run), [])
    entries.append(plan)
    atomic_json(ledger, history)
    try:
        frame.at[run,'status'] = op.SUBMITTING
        frame.at[run,'job_id'] = ''
        frame.at[run,'attempts'] = attempt
        frame.at[run,'progress'] = 0.0
        frame.at[run,'submitted_at'] = backend.pd.NaT
        frame.at[run,'updated_at'] = op.utc_now()
        frame.at[run,'message'] = f'OOM retry being submitted: {memory} MiB; check Slurm before retrying'
        publish_one_row(op, backend.state_file, run, frame, {old_job,''})
        if archived:
            if Path(archived).exists():
                raise UnsafeRetry('Archive path already exists')
            old_log.rename(archived)
        plan['status'] = 'submitting'
        atomic_json(ledger, history)
        job.submit(partition=backend.partition, qos=backend.partition)
        job_id = op.extract_job_id(job.submit_message)
        if not job_id:
            raise UnsafeRetry('Submission returned no Slurm job ID')
        # Save receipt BEFORE updating H5, so an interruption cannot hide an accepted job.
        plan.update(status='submitted', job_id=job_id)
        atomic_json(audit/'accepted.json', plan)
        atomic_json(ledger, history)
        frame.at[run,'status'] = op.SUBMITTED
        frame.at[run,'job_id'] = job_id
        frame.at[run,'submitted_at'] = op.utc_now()
        frame.at[run,'updated_at'] = op.utc_now()
        frame.at[run,'message'] = f'OOM retry: Slurm {job_id}, {memory} MiB, one CPU; previous job {old_job}'
        publish_one_row(op, backend.state_file, run, frame, {old_job,''})
        atomic_json(audit/'receipt.json', plan)
        return job_id
    except BaseException as error:
        plan['status'] = 'uncertain'
        plan['error'] = f'{type(error).__name__}: {error}'[:500]
        atomic_json(ledger, history)
        atomic_json(audit/'uncertain.json', plan)
        raise UnsafeRetry(f'Submission interrupted for {run:06d}; inspect {audit} and Slurm before continuing') from error


def run_cycle(backend, policy, ledger, audit_root, submit=False, max_submit=1):
    results = []
    submitted = 0
    accepted_ids = set()
    op = backend.op
    with op.state_lock(str(backend.state_file)):
        if not backend.state_file.is_file():
            raise UnsafeRetry('State file is missing; OOM listener will not restore or create it')
        frame = op.load_state(str(backend.state_file))
        history = load_history(ledger, backend.state_file, audit_root)
        failed = list(frame.index[frame.status.eq(op.FAILED)])
        jobs = sorted({str(frame.at[n,'job_id']) for n in failed if str(frame.at[n,'job_id']).isdecimal()})
        records = backend.accounting(jobs)
        for n in failed:
            run = int(n)
            row = frame.loc[n]
            job_id = str(row['job_id'])
            entries = history['runs'].get(str(run), [])
            reason = None
            if not backend.allowed(row):
                reason = 'excluded_source'
            elif not confirmed_oom(records, job_id, run):
                reason = 'not_confirmed_oom'
            elif any(e['failed_job_id'] == job_id for e in entries):
                reason = 'already_journaled'
            elif backend.pd.isna(row['updated_at']) or (op.utc_now()-row['updated_at']).total_seconds() < policy.cooldown_seconds:
                reason = 'cooldown'
            if reason:
                results.append({'run':run,'action':reason}); continue
            previous = memory_mib(records[job_id]['memory'])
            memory = policy.next_memory(previous, len(entries))
            if memory is None:
                results.append({'run':run,'action':'memory_or_retry_limit','previous_memory_mib':previous}); continue
            queue = backend.queue()
            if any(q[0] == job_id or q[1].startswith(f'{run:06d}-') for q in queue):
                results.append({'run':run,'action':'active_job'}); continue
            queue_ids = {q[0] for q in queue}
            if len(queue) + len(accepted_ids-queue_ids) >= backend.max_jobs:
                results.append({'run':run,'action':'queue_full'}); continue
            missing = backend.check_inputs(run)
            if missing:
                results.append({'run':run,'action':'waiting_for_input','missing':missing}); continue
            plan = {'run':run,'failed_job_id':job_id,'previous_memory_mib':previous,'memory_mib':memory}
            if not submit:
                results.append({**plan,'action':'would_submit'}); continue
            if submitted >= max_submit:
                results.append({**plan,'action':'cycle_limit'}); continue
            # Recheck after input I/O, immediately before the mutation.
            queue = backend.queue()
            queue_ids = {q[0] for q in queue}
            if len(queue)+len(accepted_ids-queue_ids) >= backend.max_jobs or any(q[0] == job_id or q[1].startswith(f'{run:06d}-') for q in queue):
                results.append({**plan,'action':'queue_changed'}); continue
            new_job = submit_retry(backend, frame, run, previous, memory, history, ledger, audit_root)
            submitted += 1
            accepted_ids.add(new_job)
            results.append({**plan,'action':'submitted','job_id':new_job})
    return results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--state-file',type=Path)
    parser.add_argument('--ledger',type=Path)
    parser.add_argument('--audit-root',type=Path)
    parser.add_argument('--memory-steps-mib',default='32768,49152')
    parser.add_argument('--max-retries',type=int,default=2)
    parser.add_argument('--cooldown-seconds',type=int,default=60)
    parser.add_argument('--poll-seconds',type=float,default=60)
    parser.add_argument('--max-submit-per-cycle',type=int,default=1)
    parser.add_argument('--submit',action='store_true')
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.max_submit_per_cycle <= 0:
        parser.error('poll-seconds and max-submit-per-cycle must be positive')
    if not args.config.is_file() or not (args.repo/'reprox/online_processing.py').is_file():
        parser.error('Existing reprox checkout and configuration are required')
    try:
        args.policy = Policy(tuple(int(x) for x in args.memory_steps_mib.split(',')), args.max_retries, args.cooldown_seconds)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s UTC %(levelname)s %(message)s')
    for handler in logging.getLogger().handlers:
        handler.formatter.converter = time.gmtime
    backend = ReproxBackend(args.repo,args.config,args.state_file)
    ledger = args.ledger or Path(str(backend.state_file)+'.oom-retries.json')
    audit = args.audit_root or backend.state_file.parent/'oom_retry_attempts'
    LOG.info('Start OOM listener: state=%s submit=%s memory_steps_mib=%s max_retries=%s',
             backend.state_file,args.submit,args.policy.memory_steps,args.policy.max_retries)
    try:
        while True:
            results = run_cycle(backend,args.policy,ledger,audit,args.submit,args.max_submit_per_cycle)
            LOG.info('Cycle: %s',json.dumps(results,sort_keys=True))
            if args.once:
                return 0
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        LOG.info('Listener stopped')
        return 130
    except Exception:
        LOG.exception('OOM listener stopped; inspect state, ledger and Slurm before restarting')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
