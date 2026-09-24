#!/usr/bin/env python3
"""Preview or submit one SR3 reprox processing job on Midway."""

import argparse
import os


# Update this path when using a different checkout.
CONFIG_PATH = "/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini"

if not os.path.isfile(CONFIG_PATH):
    raise FileNotFoundError(f"Config file not found. Update CONFIG_PATH: {CONFIG_PATH}")
os.environ["REPROX_CONFIG"] = CONFIG_PATH

from reprox import core, submit_jobs  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Run number, for example 087209")
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
    tag = core.config["processing"]["container_tag"]
    partition = core.config["processing"]["allowed_partitions"].split(",")[0].strip()
    job = submit_jobs._make_job(
        run_name=run_id,
        targets=" ".join(args.targets),
        base_folder=base_folder,
        context=core.config["context"]["context"],
        package=core.config["context"]["package"],
        ram=int(core.config["processing"]["ram"]),
        cpus_per_task=int(core.config["processing"]["cpus_per_job"]),
        container=f"xenonnt-{tag}.simg",
        context_config_kwargs=core.configured_context_kwargs(),
    )

    print("Single-run job command:\n")
    print(job.submit_kwargs["jobstring"])
    print(f"Targets: {', '.join(args.targets)}")
    print(f"Working directory: {base_folder}")
    print(f"Slurm log: {job.submit_kwargs['log']}")
    print(f"Job script: {job.submit_kwargs['sbatch_file']}")

    if not args.submit:
        print(f"\nDry run only. Add --submit to submit this job to {partition}.")
        return

    job.submit(partition=partition, qos=partition)
    print(f"Submitted run {run_id} to {partition}.")


if __name__ == "__main__":
    main()
