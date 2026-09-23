# Submit a profiling job

`submit_profile.py` submits one representative SR3 processing job to the
`lgrandi` partition. It can collect a CPU function profile, peak RAM usage, or
both. Without `--submit`, it only prints the job command and output paths.

## 1. Prepare the environment

Use the same environment or container setup that will be used for the full
reprocessing campaign. Install this reprox checkout so that the profiling
context and the updated submission code are available:

```bash
cd /home/zhut/analysis/sr3_fast/reprox
python -m pip install -e . --user
```

At the top of `scripts/submit_profile.py`, check that `CONFIG_PATH` points to
the intended ini file and `RESOURCE_CACHE` points to the existing cache:

```python
CONFIG_PATH = "/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini"
RESOURCE_CACHE = "/home/zhut/resource_cache"
```

The ini file controls the container tag, requested RAM and CPUs, targets,
prerequisites, and `base_folder`.

## 2. Preview the job

From the repository root, provide a representative run ID:

```bash
python scripts/submit_profile.py 087209
```

This prints the complete `straxer` command and all output paths. It does not
submit a Slurm job.

By default, the script profiles all targets listed in the ini file and enables
both CPU and RAM profiling.

## 3. Submit the job

After checking the preview:

```bash
python scripts/submit_profile.py 087209 --submit
```

The job is submitted with:

```text
partition = lgrandi
qos       = lgrandi
```

Check its state with:

```bash
squeue -u "$USER"
```

## Profiling options

CPU profile only:

```bash
python scripts/submit_profile.py 087209 --profile cpu --submit
```

Peak RAM only:

```bash
python scripts/submit_profile.py 087209 --profile ram --submit
```

Both CPU and peak RAM:

```bash
python scripts/submit_profile.py 087209 --profile both --submit
```

To override the targets from the ini file:

```bash
python scripts/submit_profile.py 087209 \
    --targets peak_basics peak_positions_cnf peak_proximity event_basics \
    --profile both \
    --submit
```

## Input and output isolation

The profiling context reads only the prerequisites configured in the ini file:

```ini
required_from_osg_dtypes = peaklets,lone_hits
```

Higher-level targets already stored in Rucio or another storage frontend are
hidden from the profiling context. This forces them to be recomputed without
using `straxer --from_scratch`, which would restart from `raw_records`.

Every invocation creates a short job ID in the form
`<run>-<profile>-<UTC timestamp>`, for example
`087208-both-20260923T054822Z`. The full target list remains visible in
`job.sh` and in the command preview. All files remain under the ini
`base_folder`, on the large storage volume:

```text
<base_folder>/
├── profile_jobs/
│   └── <job-id>/
│       ├── cpu.pstats
│       ├── slurm.log
│       └── job.sh
└── profile_strax_data/
    └── <job-id>/
        └── newly processed strax data
```

Separate profiling jobs therefore do not write to the same strax output
directory.

The job runs from the directory containing `RESOURCE_CACHE`, so straxen finds
the existing cache as `./resource_cache`. Strax output and profiling artifacts
still use absolute paths under `base_folder`; changing the working directory
does not move those files into the home directory.

## Inspect the results

Follow the Slurm log:

```bash
tail -f <base_folder>/profile_jobs/<job-id>/slurm.log
```

The log contains normal `straxer` progress messages and the peak RAM result.

For CPU profiling, inspect `cpu.pstats` with `snakeviz`:

```bash
snakeviz <base_folder>/profile_jobs/<job-id>/cpu.pstats
```

Or print the most expensive functions without a GUI:

```bash
python -m pstats <base_folder>/profile_jobs/<job-id>/cpu.pstats
```

CPU profiling requires `yappi` in the processing container. RAM profiling
requires `memory_profiler`. If either package is missing, the reason will be
shown in `slurm.log`.
