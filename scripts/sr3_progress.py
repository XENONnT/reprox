#!/usr/bin/env python3
"""Inventory SR3 data in the configured Midway context; never submit jobs."""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from tqdm import tqdm


# Set this to the absolute path of your ini file on Midway.
CONFIG_PATH = "/home/zhut/analysis/sr3_fast/reprox/reprox/reprocessing_sr3_online.ini"
if not os.path.isfile(CONFIG_PATH):
    raise FileNotFoundError(f"Config file not found. Update CONFIG_PATH: {CONFIG_PATH}")
os.environ["REPROX_CONFIG"] = CONFIG_PATH
from reprox import core  # noqa: E402


def mode_filename(mode):
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", mode).strip("_") or "unknown"
    return f"pending_{slug}.csv"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(core.config["context"]["base_folder"]) / "sr3_progress",
    )
    args = parser.parse_args()

    targets = core.configured_targets()
    prerequisites = [x.strip() for x in
                     core.config["prerequisites"]["required_from_osg_dtypes"].split(",")]
    data_types = list(dict.fromkeys(prerequisites + targets))
    # RunDB's minimum_run_number uses >; include the requested first SR3 run.
    first_run = int(core.config["context"]["minimum_run_number"])
    st = core.get_context(minimum_run_number=first_run - 1)
    runs = st.select_runs().copy()
    required_columns = {"name", "number", "mode", "start"}
    if not required_columns.issubset(runs.columns):
        raise ValueError(f"Run table lacks {sorted(required_columns - set(runs.columns))}")
    runs = runs[runs["number"].astype(int) >= first_run].copy()
    if runs.empty:
        raise ValueError(f"No runs found with number >= {first_run}")
    runs["name"] = runs["name"].astype(str).str.zfill(6)
    runs["mode"] = runs["mode"].fillna("unknown").astype(str)
    runs["start"] = pd.to_datetime(runs["start"], utc=True)
    runs = runs.sort_values(["start", "number"]).reset_index(drop=True)

    for data_type in data_types:
        runs[data_type] = [bool(st.is_stored(run_id, data_type)) for run_id in
                           tqdm(runs["name"], desc=f"Checking {data_type}")]
    runs["prerequisites_ready"] = runs[prerequisites].all(axis=1)
    runs["processed"] = runs[targets].all(axis=1)
    runs["processed_targets"] = runs[targets].sum(axis=1)
    runs["status"] = "partial"
    runs.loc[runs["processed_targets"] == 0, "status"] = "unprocessed"
    runs.loc[runs["processed"], "status"] = "complete"
    runs["missing_targets"] = runs.apply(
        lambda row: ",".join(t for t in targets if not row[t]), axis=1)
    runs["start_date"] = runs["start"].dt.strftime("%Y-%m-%d")

    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    runs.to_csv(out / "run_availability.csv", index=False)

    per_mode = runs.groupby("mode", dropna=False).agg(
        total=("name", "size"), complete=("processed", "sum"),
        prerequisites_ready=("prerequisites_ready", "sum"),
        unprocessed=("status", lambda x: (x == "unprocessed").sum()),
        partial=("status", lambda x: (x == "partial").sum()),
        **{f"stored_{t}": (t, "sum") for t in data_types},
    ).reset_index()
    per_mode["complete_fraction"] = per_mode["complete"] / per_mode["total"]
    per_mode.to_csv(out / "progress_by_mode.csv", index=False)

    daily = runs.groupby("start_date", dropna=False).agg(
        total=("name", "size"), complete=("processed", "sum"),
        **{f"stored_{t}": (t, "sum") for t in data_types},
    ).reset_index().sort_values("start_date")
    daily["complete_fraction"] = daily["complete"] / daily["total"]
    daily["cumulative_total"] = daily["total"].cumsum()
    daily["cumulative_complete"] = daily["complete"].cumsum()
    daily["cumulative_fraction"] = daily["cumulative_complete"] / daily["cumulative_total"]
    daily.to_csv(out / "progress_by_start_date.csv", index=False)

    pending_files = {}
    for mode, group in runs[~runs["processed"]].groupby("mode"):
        filename = mode_filename(mode)
        if filename in pending_files.values():
            raise ValueError(f"Two run modes map to the same filename: {filename}")
        group.to_csv(out / filename, index=False)
        pending_files[mode] = filename
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "config": os.path.abspath(os.environ["REPROX_CONFIG"]),
        "package": core.config["context"]["package"],
        "context": core.config["context"]["context"],
        "context_kwargs": core.configured_context_kwargs(),
        "first_run": first_run,
        "targets": targets,
        "prerequisites": prerequisites,
        "pending_files": pending_files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {len(runs)} runs and {len(pending_files)} mode lists to {out}")


if __name__ == "__main__":
    main()
