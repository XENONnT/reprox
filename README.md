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

```python
import pandas as pd
runs = pd.read_hdf("/path/to/state/file.h5", key="runs")
```

`submitting` is not retried automatically because Slurm may have accepted the
job before its ID was saved. Inspect Slurm before changing that state.

Retry all runs that were already `failed` when the listener starts:

```bash
PYTHONPATH=. python -m reprox.online_processing \
  --once \
  --submit \
  --retry-failed \
  --state-file "$STATE_FILE"
```

The reset happens only once at startup, retains the attempt count, and archives
the old log before resubmission. A failed row whose log already has a clean
completion marker is repaired to `completed` instead of being resubmitted.

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

Both programs use the same `.lock` file to prevent simultaneous HDF5 writes.
The current lock covers the entire cycle, including slow Rucio checks and
filesystem moves. It prevents corruption but can make the other listener wait
for a long time. Therefore, do not run both listeners continuously at the same
time: stop the processing listener, run validation with `--once`, then restart
processing. A leftover empty `.lock` file is normal; the kernel lock is
released automatically when the process exits.
