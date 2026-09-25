# ReProx: ReProcessing for XENONnT
 Package | CI |
| --- | --- |
|[![Documentation Status](https://readthedocs.org/projects/reprox/badge/?version=latest)](https://reprox.readthedocs.io/en/latest/?badge=latest) | [![Test package](https://github.com/XENONnT/reprox/actions/workflows/pytest.yml/badge.svg?branch=master)](https://github.com/XENONnT/reprox/actions/workflows/pytest.yml)| 
|[![PyPI version shields.io](https://img.shields.io/pypi/v/reprox.svg)](https://pypi.python.org/pypi/reprox/) | [![Coverage Status](https://coveralls.io/repos/github/XENONnT/reprox/badge.svg?branch=master)](https://coveralls.io/github/XENONnT/reprox?branch=master)| 
|[![Python Versions](https://img.shields.io/pypi/pyversions/reprox.svg)](https://pypi.python.org/pypi/reprox)| [![CodeFactor](https://www.codefactor.io/repository/github/xenonnt/reprox/badge)](https://www.codefactor.io/repository/github/xenonnt/reprox)|
|[![PyPI downloads](https://img.shields.io/pypi/dm/reprox.svg)](https://pypistats.org/packages/reprox)| |




## Documentation
Please visit [the documentation](https://reprox.readthedocs.io/en/latest/?badge=latest) for installation instructions and examples.

## Examples
Can be found either [on github](https://github.com/XENONnT/reprox/blob/master/EXAMPLES.md) or the [online documentation](https://reprox.readthedocs.io/en/latest/reference/examples.html).

## Online processing

`reprox-online-processing` discovers completed TPC runs in RunDB, waits for
local Rucio prerequisites, submits jobs, and tracks Slurm and straxer logs. A
job is complete when its log contains `Processing job ended` without a detected
error. The marker is only written after straxer exits successfully.

When a clean log contains `This data is already available. Straxer is done`,
the listener trusts the availability check performed inside the job container.
It looks only for directories named `<run_number>-*`, excluding `_temp`
directories, without matching targets, computing lineage, or checking metadata.
It records `moved` if run output exists in `destination_folder`, or `completed`
if it exists only in `base_folder`. Destination takes precedence when both
folders contain run output. Both statuses set progress to 100%. If neither
folder contains run output, monitoring marks the run `failed`, even if the log
also contains `Processing job ended`. Failed-run recovery uses the same location
check; `--retry-failed` resets unresolved runs for retry.

From the repository root:

```bash
export REPROX_CONFIG="$PWD/reprox/reprocessing_sr3_online.ini"
STATE_FILE=/path/to/state/file.h5
```

The default state file is `<base_folder>/online_processing.h5`, which is the
recommended location. Omit `--state-file` to use that default. Always reuse the
same file when restarting; using a new file can rediscover and resubmit runs.

Check one cycle without submitting:

```bash
PYTHONPATH=. python -m reprox.online_processing \
  --once \
  --state-file "$STATE_FILE"
```

Run continuously with submission enabled:

```bash
PYTHONPATH=. python -u -m reprox.online_processing \
  --submit \
  --poll-seconds 60 \
  --max-submit-per-cycle 1 \
  --state-file "$STATE_FILE"
```

Use `tmux` for a listener that should survive SSH disconnection:

```bash
tmux new -s reprox-processing
# Run the continuous command above, then detach with Ctrl-b d.
```

Set the ini `run_mode` to a comma-separated list of exact RunDB mode names to
restrict discovery to those modes. Leave it empty to monitor all modes.
`--max-submit-per-cycle` limits each cycle, while the ini `max_jobs` limits all
jobs under the current username.

Processing states are:

```text
waiting_for_input -> ready_to_submit -> submitting -> submitted -> processing -> completed
                                                                  -> failed
waiting_for_input / ready_to_submit -> skipped
```

The HDF5 table stores one row per run, including prerequisites, status,
progress, targets, Slurm job ID, attempts, timestamps, and the latest message:

Each configured prerequisite is stored as its own boolean column. For example,
the TPC state has `peaklets` and `lone_hits`, while the neutron-veto state has
`raw_records_nv`. If an existing HDF5 file has different prerequisite columns
from the selected ini file or `--prerequisites`, the listener reports a schema
error and exits instead of modifying or replacing the file.

```python
import pandas as pd
runs = pd.read_hdf("/path/to/state/file.h5", key="runs")
```

## Validate and move SR3 output

`reprox-online-validation` reads the same state file as online processing. It
shallow-validates completed output in `base_folder`, moves it to
`destination_folder`, and records `validating -> moving -> moved`. It exits
without moving if the two directories are on different filesystems. A normal
move preserves owner, group, and permissions; `--group` is an optional
override.

Run one validation/move cycle:

```bash
export REPROX_CONFIG="$PWD/reprox/reprocessing_sr3_online.ini"
STATE_FILE=/path/to/state/file.h5
PYTHONPATH=. python -m reprox.online_validation \
  --once \
  --max-runs-per-cycle 1 \
  --state-file "$STATE_FILE"
```

## Online processing Q&A

### How do I process TPC, neutron-veto, and muon-veto data for the same run?

Run one listener per detector with its own ini file. Linked detector data may
share a RunDB run number, but each listener records that number in a separate
HDF5 state file and writes logs and output below a separate `base_folder`.

| Detector | Config | Target | Required Rucio input | Base folder |
| --- | --- | --- | --- | --- |
| TPC | `reprocessing_sr3_online.ini` | `event_info` | `peaklets,lone_hits` | `.../sr3/` |
| Neutron veto | `reprocessing_sr3_online_nv.ini` | `events_nv` | `raw_records_nv` | `.../sr3_nv/` |
| Muon veto | `reprocessing_sr3_online_mv.ini` | `events_mv` | `raw_records_mv` | `.../sr3_mv/` |

For example, start the neutron-veto listener from the repository root with:

```bash
export REPROX_CONFIG="$PWD/reprox/reprocessing_sr3_online_nv.ini"
PYTHONPATH=. python -u -m reprox.online_processing \
  --submit \
  --poll-seconds 60 \
  --max-submit-per-cycle 1
```

Omitting `--state-file` uses `<base_folder>/online_processing.h5`, so these
three configurations automatically use independent state files. Activate the
same detector-specific config when running `reprox.online_validation` for that
state file.

### How do I retry failed runs?

Stop any existing listener, then start one cycle with `--retry-failed`:

```bash
PYTHONPATH=. python -m reprox.online_processing \
  --once \
  --submit \
  --retry-failed \
  --state-file "$STATE_FILE"
```

At startup, this resets existing `failed` rows to `waiting_for_input`, retains
their attempt counts, and archives old logs before resubmission. If an old log
already contains a clean `Processing job ended` marker, the row is repaired to
`completed` instead. An already-available log recovers to `completed` or `moved`
according to run output location as described above. This reset is performed only
once per program start.

A row left in `submitting` is not retried automatically because Slurm may have
accepted the job before its job ID was written to the state file. Check Slurm
and the job log before changing such a row.

### Why was a run marked as failed even though it left the Slurm queue?

Leaving the queue is not sufficient evidence that processing succeeded. A run
needs a clean `Processing job ended` marker or an already-available message with
located run output. On restart, the listener checks existing submitted and
processing runs again, so a clean completion marker can repair a stale state.

### What happens when I change `excluded_sources`?

The ini file is read when the listener starts, so restart the listener after
changing it. Source names are comma-separated. For example:

```ini
excluded_sources = kr-83m,th-232
```

Adding a source changes matching runs in `waiting_for_input` or
`ready_to_submit` to `skipped` on the next cycle. It does not alter runs that
are already submitted, processing, completed, validating, or moved.

Removing a source affects newly discovered runs, but existing `skipped` rows
are not automatically restored. To reconsider those rows, their status must be
reset deliberately after confirming that they were skipped by this policy.
Do not delete the entire state file just to clear skipped rows.

Except for the special Kr-83m handling, an excluded source must exactly match
the RunDB `source` value after lower-casing and trimming whitespace. It does not
match arbitrary text inside `mode`.

### How are state-file backups created and restored?

The processing listener creates a backup after its first successful cycle and
then every 10 successful cycles. It retains three rotating copies next to the
state file:

```text
online_processing.h5.backup-1  # newest
online_processing.h5.backup-2
online_processing.h5.backup-3
```

If the main HDF5 file is missing, the newest readable backup is restored
automatically. Validation also creates a backup after every successful
validation/move cycle. A corrupt main file is not replaced automatically; move
it aside first if you deliberately want startup to restore a backup.

Use `--backup-every-cycles` to change the processing backup interval and
`--backup-count` to change the number retained. An interval of zero disables
new processing backups.

These adjacent backups protect against deletion or corruption of the main HDF5
file, but not against deletion of the whole directory or loss of its filesystem.
For stronger protection, copy the state file and its backups to another
filesystem periodically.

### What if the state file and every backup are lost?

Do not immediately restart submission with a new empty state file. Runs already
moved to `destination_folder` still exist, but the listener has lost their
record and may rediscover and resubmit them. First restore an external backup
or reconstruct the state from Slurm logs and the destination contents.

### Why does the other listener appear stuck?

Processing and validation share `<state-file>.lock`, and each currently holds
the lock for its entire cycle. The second program may therefore wait while the
first performs slow Rucio checks or filesystem operations. Use one continuous
processing listener, stop it for a validation `--once` cycle, and then restart
it. The empty `.lock` file itself is harmless and should not be deleted while a
listener may be running.
