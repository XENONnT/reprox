# Bounded automatic retries for Slurm OOM failures

The OOM listener uses the same configuration, submission chain and state lock
as online processing and validation. It watches one detector's state file;
use a separate process and configuration for another detector.

## Policy

- Consider only `failed` rows whose current job ID has an explicit Slurm
  `OUT_OF_MEMORY` accounting state. A `FAILED` parent with an OOM batch step
  also qualifies. Cancellation, timeouts, generic `Killed`/137 messages,
  input errors and `validation_failed` are left for review.
- Require the source job and configured job to use one CPU. Choose the next
  tier strictly above the source job's actual `ReqMem`: 32768, then 49152 MiB
  (32, then 48 GiB). Set this final request after the calibration multiplier.
- Permit at most two automatic retries per run. Manual attempts remain in the
  H5 `attempts` field; the retry journal counts automatic attempts separately.
- Poll every 60 seconds, wait at least 60 seconds after failure, submit at most
  one retry per cycle and respect the existing `max_jobs` and excluded sources.
  Recheck the user's queue and reject any active job for the same run.
- Confirm configured local prerequisites through the existing input context,
  completed metadata and every named chunk's existence, size and nonzero length.
  The worker container determines whether the output is already available.

## Run from a checkout

Export the detector configuration **before** importing reprox, launching the
module or using the installed `reprox-online-oom-retry` command. Use the same
checkout and environment as the existing processing listener, with `squeue`,
`sacct` and `sbatch` available.

```bash
export REPROX_CONFIG="$PWD/reprox/reprocessing_sr3_online.ini"
# Preview one cycle; no submissions or state/log/journal changes:
PYTHONPATH=. python -m reprox.online_oom_retry \
  --repo . --config "$REPROX_CONFIG" --once
# Continuous retries:
PYTHONPATH=. python -u -m reprox.online_oom_retry \
  --repo . --config "$REPROX_CONFIG" \
  --submit --poll-seconds 60 --max-submit-per-cycle 1
```

The installed command accepts the same arguments. Use
`--memory-steps-mib 32768,49152`, `--max-retries 2` and
`--cooldown-seconds 60` to customize the limits. `--state-file` may select a
file inside the configured `base_folder`; it cannot select another detector's
folder. State-schema checks remain those of online processing.

## Journal, backups and interruptions

The default journal is `<base_folder>/online_processing.h5.oom-retries.json`.
Audit folders are `<base_folder>/oom_retry_attempts/<run>.<UTC>.<unique-id>/`.
Keep this journal and the audit folders across listener restarts and H5 backup
restoration. Do not include these runtime records, H5 files or logs in Git.

Each retry preserves a read-only H5 snapshot, the previous log and the original
worker command. The old log receives a unique archive name. Under the shared
state lock, the row changes from `failed` to `submitting`, then `submitted`.
Accepted Slurm job IDs are recorded in `accepted.json` and the journal before
H5 is updated. Existing processing and validation then track the new job and
validate/move its outputs.

If submission is interrupted, the reply is uncertain or H5 publication fails,
the listener stops and preserves an unresolved journal entry. Restart refuses
unresolved or corrupt entries, a missing journal with existing audit plans,
and missing/corrupt H5 files. Check Slurm, `accepted.json`, `uncertain.json`,
the journal and the H5 row before resolving the entry. Never clear the journal
or reset `submitting` to force another submission.

After the retry or memory limit is reached, the failed row remains for manual
review. The listener does not delete raw data, old failed output or temporary
directories.

## Tests

`tests/test_online_oom_retry.py` exercises OOM classification, strict memory
increases, queue duplication/capacity/visibility lag, missing inputs, cooldown,
restored old H5 state, journal loss/corruption, lost submission replies,
post-acceptance publication failure and real HDF5 readback. Scheduler calls
are simulated; state writes occur only in temporary directories.

```bash
PYTHONPATH=. python -m pytest tests/test_online_oom_retry.py
```
