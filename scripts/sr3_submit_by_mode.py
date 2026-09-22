#!/usr/bin/env python3
"""Submit pending SR3 runs by mode to lgrandi from sr3_progress.py output."""

import argparse
import json
import os
import time
from pathlib import Path

import pandas as pd


# Set this to the absolute path of your ini file on Midway.
CONFIG_PATH = "/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini"
if not os.path.isfile(CONFIG_PATH):
    raise FileNotFoundError(f"Config file not found. Update CONFIG_PATH: {CONFIG_PATH}")
os.environ["REPROX_CONFIG"] = CONFIG_PATH
from reprox import core, submit_jobs  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", type=Path,
        default=Path(core.config["context"]["base_folder"]) / "sr3_progress",
    )
    parser.add_argument("--submit", action="store_true",
                        help="Actually submit jobs; otherwise print the plan only")
    parser.add_argument("--limit-per-mode", type=int, default=0,
                        help="Maximum jobs per mode; 0 means all eligible runs")
    args = parser.parse_args()
    if args.limit_per_mode < 0:
        parser.error("--limit-per-mode must be nonnegative")

    directory = args.input_dir.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    targets = core.configured_targets()
    prerequisites = [x.strip() for x in
                     core.config["prerequisites"]["required_from_osg_dtypes"].split(",")]
    if (manifest["targets"] != targets or manifest["prerequisites"] != prerequisites or
            manifest["package"] != core.config["context"]["package"] or
            manifest["context"] != core.config["context"]["context"] or
            manifest["context_kwargs"] != core.configured_context_kwargs() or
            manifest["first_run"] != int(core.config["context"]["minimum_run_number"])):
        raise ValueError("Progress snapshot and active reprox configuration differ")

    # Recheck availability because the CSVs may be older than the current storage state.
    st = core.get_context(minimum_run_number=manifest["first_run"] - 1)
    selected = {}
    for mode, filename in sorted(manifest["pending_files"].items()):
        path = (directory / filename).resolve()
        if path.parent != directory:
            raise ValueError(f"Invalid pending file path: {filename}")
        frame = pd.read_csv(path, dtype={"name": str})
        if not {"name", "mode"}.issubset(frame.columns):
            raise ValueError(f"Missing name or mode column in {path}")
        if not frame["mode"].eq(mode).all():
            raise ValueError(f"Mode mismatch in {path}")
        run_ids = []
        for run_id in frame["name"].str.zfill(6):
            if not st.is_stored(run_id, prerequisites):
                continue
            if st.is_stored(run_id, targets):
                continue
            run_ids.append(run_id)
        if args.limit_per_mode:
            run_ids = run_ids[:args.limit_per_mode]
        selected[mode] = run_ids
        print(f"{mode}: {len(run_ids)} eligible runs")

    if not args.submit:
        print("Dry run only. Pass --submit to send these jobs to lgrandi.")
        return

    tag = core.config["processing"]["container_tag"]
    for mode, run_ids in selected.items():
        print(f"Submitting {mode}: {len(run_ids)} runs")
        for run_id in run_ids:
            while not submit_jobs.can_submit_more_jobs():
                time.sleep(60)
            job = submit_jobs._make_job(
                run_name=run_id,
                targets=" ".join(targets),
                base_folder=core.config["context"]["base_folder"],
                context=core.config["context"]["context"],
                package=core.config["context"]["package"],
                ram=int(core.config["processing"]["ram"]),
                cpus_per_task=int(core.config["processing"]["cpus_per_job"]),
                container=f"xenonnt-{tag}.simg",
                context_config_kwargs=core.configured_context_kwargs(),
            )
            job.submit(partition="lgrandi", qos="lgrandi")
            print(f"Submitted {mode} {run_id}")
            time.sleep(1)


if __name__ == "__main__":
    main()
