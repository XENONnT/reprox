# SR3 single-run submission scripts

The scripts in this directory submit one SR3 processing job using the
partition configured in `reprocessing_sr3_online.ini`:

- `submit_single.py` processes one run using the normal shared reprox output.
- `submit_profile.py` processes one run in an isolated output directory with
  CPU profiling, detailed RAM profiling, or both.

Both scripts only preview the job unless `--submit` is provided.

## Setup

Install this reprox checkout in the submission environment:

```bash
cd /home/zhut/analysis/sr3_fast/reprox
python -m pip install -e . --user
```

At the top of each script, check the explicit paths:

```python
CONFIG_PATH = "/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini"
```

`submit_profile.py` also has a `RESOURCE_CACHE` setting because profiling jobs
use a private output directory while running from the existing cache location.

The ini file controls the container tag, requested RAM and CPUs, default
targets, prerequisites, and `base_folder`.

## Submit one run

Preview the command and output paths:

```bash
python scripts/submit_single.py 087209
```

Submit it after checking the preview:

```bash
python scripts/submit_single.py 087209 --submit
```

Override the targets from the ini file when needed:

```bash
python scripts/submit_single.py 087209 \
    --targets peak_basics peak_positions_cnf peak_proximity event_basics \
    --submit
```

The single-run script follows the normal reprox layout. It runs from
`base_folder`, uses the package and context from the ini file, and writes its
job files and processing output to the shared reprox directories:

```text
<base_folder>/
├── job_logs/<run>.txt
├── job_scripts/<run>-<targets>.sh
└── strax_data/
    └── processed strax data
```

The partition and QoS are read from the first entry in the ini
`allowed_partitions` setting.

## Submit a profiling run

Preview a profiling job:

```bash
python scripts/submit_profile.py 087209
```

Submit it after checking the preview:

```bash
python scripts/submit_profile.py 087209 --submit
```

The default profile mode is `both`. Select one mode explicitly with:

```bash
# CPU profile plus straxer's normal RAM logging
python scripts/submit_profile.py 087209 --profile cpu --submit

# More frequent RAM sampling with memory_profiler
python scripts/submit_profile.py 087209 --profile ram --submit

# CPU profile and more frequent RAM sampling in one Slurm job
python scripts/submit_profile.py 087209 --profile both --submit
```

Override the targets when needed:

```bash
python scripts/submit_profile.py 087209 \
    --targets peak_basics peak_positions_cnf peak_proximity event_basics \
    --profile cpu \
    --submit
```

Profiling output uses separate private directories:

```text
<base_folder>/
├── profile_jobs/
│   └── <run>-<profile>-<UTC timestamp>/
│       ├── cpu.pstats
│       ├── slurm.log
│       └── job.sh
└── profile_strax_data/
    └── <run>-<profile>-<UTC timestamp>/
        └── newly processed strax data
```

`cpu.pstats` is created for the `cpu` and `both` modes. Inspect it with:

```bash
snakeviz <base_folder>/profile_jobs/<job-id>/cpu.pstats
```

or:

```bash
python -m pstats <base_folder>/profile_jobs/<job-id>/cpu.pstats
```

CPU profiling requires `yappi`. The `ram` and `both` modes require
`memory_profiler`. Normal straxer progress logs include sampled RAM usage even
when only CPU profiling is enabled.

## Profiling input and output isolation

The profiling script reads only the prerequisites configured in the ini file,
normally:

```ini
required_from_osg_dtypes = peaklets,lone_hits
```

For profiling, `reprox.contexts.xenonnt_online` passes these data types to the
non-output storage frontends through `take_only`. This reprox context is only
for isolated tests. Normal processing, including `submit_single.py`, uses
`cutax.contexts.xenonnt_online` from the ini file.

Existing higher-level targets in Rucio and other storage frontends are hidden
from the processing context. This forces the requested targets to be
recomputed without `straxer --from_scratch`, which would restart from
`raw_records`.

Each profiling job uses an independent strax output directory, so repeated
profiling submissions cannot write to the same output. Its shell runs from the
directory containing `RESOURCE_CACHE`, allowing straxen to reuse
`./resource_cache`. All profiling data, logs, and job files use absolute paths
under `base_folder`.

Check submitted jobs with:

```bash
squeue -u "$USER"
```
