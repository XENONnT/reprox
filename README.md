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

`reprox-online-processing` monitors recent completed TPC runs in RunDB, checks
whether the configured prerequisites are complete on the local Rucio mount,
tracks submitted jobs, extracts processing progress from straxer logs, and
records completion.

Select the SR3 configuration before starting it:

```bash
export REPROX_CONFIG=/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini
```

Run one monitoring cycle without submitting jobs:

```bash
reprox-online-processing --once
```

Monitor continuously without submitting:

```bash
reprox-online-processing
```

Enable state-driven submission explicitly:

```bash
reprox-online-processing --submit
```

By default, at most one ready run is submitted per cycle. Change this limit
with `--max-submit-per-cycle`; zero uses all capacity below the ini `max_jobs`
limit.
For a one-run submission test, use `--once --submit --max-submit-per-cycle 1`.

Set the ini `run_mode` to a comma-separated list of exact RunDB mode names to
restrict discovery to those modes. Leave it empty to monitor all modes.

The state machine is:

```text
waiting_for_input -> ready_to_submit -> submitting -> submitted -> processing -> completed
                                                                  -> failed
waiting_for_input / ready_to_submit -> skipped
```

The default state file is `<base_folder>/online_processing.h5`, with
`run_number` as its integer index. There is no separate string run ID column;
the six-digit string is created only when calling strax or Slurm. Read the
table with:

```python
import pandas as pd

runs = pd.read_hdf(
    "/scratch/midway3/zhut/strax_data/sr3/online_processing.h5",
    key="runs",
)
```

The table contains RunDB metadata, local `peaklets` and `lone_hits`
availability, state, straxer progress percentage, targets, Slurm job ID,
submission attempts, timestamps, and the latest status message. The file is
rewritten through a temporary HDF5 file and atomically replaced.

After Slurm accepts a job, the returned job ID is saved to HDF5 immediately.
On restart, use the same state file: submitted runs are checked by job ID with
`squeue`. `PENDING` remains `submitted`, while `RUNNING` becomes `processing`.
Runs already marked `submitted` or `processing` are not submitted again.
Runs still waiting for input or submission use the current ini `targets`, so
changing the configured target also updates those rows on the next cycle.
Explicitly submitted Kr runs use the requested targets; reprox does not
automatically change `event_info` to `event_info_double`.
The SR3 ini excludes `kr-83m` from this online campaign. These runs remain
visible in the HDF5 table as `skipped`, including runs already waiting or ready
when the policy is applied. Jobs already submitted continue to be monitored.

The `submitting` state covers an interruption between starting submission and
saving the returned job ID. It is not automatically retried, because Slurm may
have accepted the job. Check Slurm and the output for that run before changing
its state. Stop the previous service before starting another writer for the
same HDF5 file.

Prerequisite availability is checked with a context whose only storage is
`RucioLocalFrontend` at the ini `_rucio_local_path`. This requires the metadata
and every declared chunk to exist on the mounted local Rucio path. admix
catalog or replication-rule status alone does not provide that guarantee.

## Validate and move SR3 output

After a run reaches `completed` in the HDF5 table, check its output directories
without moving them:

```bash
cd /home/zhut/analysis/sr3_fast/reprox
export REPROX_CONFIG=$PWD/reprox/reprocessing_sr3_online.ini
PYTHONPATH=. python bin/reprox-move-folders --run 087210 --deep --check-only
```

When every directory reports `ok`, move that run:

```bash
PYTHONPATH=. python bin/reprox-move-folders --run 087210 --deep --group zhut
```

`--deep` checks the lineage against the configured context and reads every
output chunk. Run it in the same software environment used to process the run,
so the lineage comparison uses the same plugin versions. The command creates
the configured `destination_folder` if it
does not exist. Use `--group zhut` on this server because the old default
`xenon1t-admins` group is not available to this user. Omitting `--run` keeps
the legacy behavior of scanning and moving every output directory.

The HDF5 status remains `completed` after a move; validation and movement are
not yet recorded as separate states. Continue using the same HDF5 state file
when restarting online processing, since the source output is no longer in
`strax_data` after the move.
