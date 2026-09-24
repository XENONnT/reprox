#!/usr/bin/env python3
"""Preview or submit one representative reprox profiling job on Midway."""

import argparse
import os
import shlex
from datetime import datetime, timezone


# Set this to the absolute path of your ini file on Midway.
CONFIG_PATH = "/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini"
PARTITION = "lgrandi"
RESOURCE_CACHE = "/home/zhut/resource_cache"

if not os.path.isfile(CONFIG_PATH):
    raise FileNotFoundError(f"Config file not found. Update CONFIG_PATH: {CONFIG_PATH}")
if not os.path.isdir(RESOURCE_CACHE):
    raise FileNotFoundError(
        f"Resource cache not found. Update RESOURCE_CACHE: {RESOURCE_CACHE}"
    )
os.environ["REPROX_CONFIG"] = CONFIG_PATH

from reprox import core, submit_jobs  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Representative run number, for example 087209")
    parser.add_argument(
        "--profile",
        choices=("cpu", "ram", "both"),
        default="both",
        help="Profiler to enable. CPU writes a pstats file; RAM prints peak memory to the log.",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=core.configured_targets(),
        help="Targets to process together. Defaults to the targets in the ini file.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the job. Without this flag, only print the command.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    run_id = f"{int(args.run_id):06d}"
    base_folder = os.path.abspath(core.config["context"]["base_folder"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_id = f"{run_id}-{args.profile}-{timestamp}"

    # Keep all large data and job artifacts on the storage selected by base_folder.
    job_dir = os.path.join(base_folder, "profile_jobs", job_id)
    output_dir = os.path.join(base_folder, "profile_strax_data", job_id)
    profile_file = os.path.join(job_dir, "cpu.pstats")
    log_file = os.path.join(job_dir, "slurm.log")
    sbatch_file = os.path.join(job_dir, "job.sh")
    os.makedirs(job_dir, exist_ok=False)

    straxer_options = ["--multi_target"]
    if args.profile in ("cpu", "both"):
        straxer_options.extend(("--profile_to", profile_file))
    if args.profile in ("ram", "both"):
        straxer_options.append("--profile_ram")

    tag = core.config["processing"]["container_tag"]
    context_kwargs = core.configured_context_kwargs().copy()
    context_kwargs["output_folder"] = output_dir
    context_kwargs["take_only"] = [
        value.strip()
        for value in core.config["prerequisites"]["required_from_osg_dtypes"].split(",")
    ]
    job = submit_jobs._make_job(
        run_name=run_id,
        targets=" ".join(args.targets),
        base_folder=base_folder,
        context="xenonnt_online",
        package="reprox",
        ram=int(core.config["processing"]["ram"]),
        cpus_per_task=int(core.config["processing"]["cpus_per_job"]),
        container=f"xenonnt-{tag}.simg",
        context_config_kwargs=context_kwargs,
        extra_straxer_options=" ".join(shlex.quote(x) for x in straxer_options),
        working_directory=os.path.dirname(RESOURCE_CACHE),
    )
    job.submit_kwargs["jobstring"] = (
        f"export REPROX_PROFILE_OUTPUT={shlex.quote(output_dir)}\n"
        + job.submit_kwargs["jobstring"]
    )
    job.submit_kwargs.update(
        log=log_file,
        jobname=f"{run_id}-profile",
        sbatch_file=sbatch_file,
    )

    print("Profiling job command:\n")
    print(job.submit_kwargs["jobstring"])
    print(f"Job directory: {job_dir}")
    print(f"Private strax output: {output_dir}")
    print(f"Resource cache: {RESOURCE_CACHE}")
    print(f"Slurm log: {log_file}")
    if args.profile in ("cpu", "both"):
        print(f"CPU profile: {profile_file}")
    if args.profile in ("ram", "both"):
        print("Peak RAM will be written to the Slurm log.")

    if not args.submit:
        print(f"\nDry run only. Add --submit to submit this job to {PARTITION}.")
        return

    job.submit(partition=PARTITION, qos=PARTITION)
    print(f"Submitted run {run_id} to {PARTITION}.")


if __name__ == "__main__":
    main()
