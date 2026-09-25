"""RunDB monitoring and state-driven online reprocessing."""

import argparse
from contextlib import contextmanager
import fcntl
import glob
import os
import re
import shutil
import subprocess
import time

import pandas as pd
import straxen
from utilix import xent_collection

from reprox import core, submit_jobs


WAITING = "waiting_for_input"
READY = "ready_to_submit"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
VALIDATING = "validating"
VALIDATION_FAILED = "validation_failed"
MOVING = "moving"
MOVED = "moved"

ACTIVE_STATES = (SUBMITTED, PROCESSING)
PREREQUISITE_STATES = (WAITING, READY)
EXCLUDED_TAGS = ("messy", "bad", "abandoned")
PROGRESS_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)% into the run")
COMPLETION_MARKER = "Processing job ended"
ALREADY_AVAILABLE_MARKER = "This data is already available. Straxer is done"
DEFAULT_BACKUP_COUNT = 3


class StateSchemaError(ValueError):
    """The selected HDF5 state file belongs to different processing inputs."""


STATE_PREFIX_COLUMNS = (
    "start",
    "end",
    "mode",
    "source",
)
STATE_SUFFIX_COLUMNS = (
    "status",
    "progress",
    "targets",
    "job_id",
    "attempts",
    "submitted_at",
    "updated_at",
    "message",
)
FIXED_STATE_COLUMNS = STATE_PREFIX_COLUMNS + STATE_SUFFIX_COLUMNS


def utc_now():
    """Return a timezone-naive UTC timestamp suitable for pandas HDF5."""
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


@contextmanager
def state_lock(path):
    """Prevent multiple reprox services from updating one state file at once."""
    lock_path = f"{os.path.abspath(path)}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def normalize_prerequisites(prerequisites=None):
    """Return validated prerequisite names in configured order."""
    if prerequisites is None:
        prerequisites = configured_prerequisites()
    prerequisites = tuple(
        str(value).strip()
        for value in prerequisites
        if str(value).strip()
    )
    if not prerequisites:
        raise ValueError("At least one prerequisite is required")
    if len(set(prerequisites)) != len(prerequisites):
        raise ValueError(f"Duplicate prerequisites are not allowed: {prerequisites}")
    collisions = set(prerequisites).intersection(FIXED_STATE_COLUMNS)
    if collisions:
        raise ValueError(
            f"Prerequisite names collide with fixed state columns: {sorted(collisions)}"
        )
    return prerequisites


def state_columns(prerequisites=None):
    prerequisites = normalize_prerequisites(prerequisites)
    return STATE_PREFIX_COLUMNS + prerequisites + STATE_SUFFIX_COLUMNS


def state_prerequisites(frame):
    """Return the prerequisite columns stored in an existing state table."""
    return tuple(column for column in frame.columns if column not in FIXED_STATE_COLUMNS)


def require_state_schema(frame, prerequisites=None):
    """Reject state files created for a different prerequisite set."""
    prerequisites = normalize_prerequisites(prerequisites)
    missing_fixed = set(FIXED_STATE_COLUMNS) - set(frame.columns)
    actual_prerequisites = state_prerequisites(frame)
    if missing_fixed or set(actual_prerequisites) != set(prerequisites):
        raise StateSchemaError(
            "State file schema does not match the selected processing inputs. "
            f"Expected prerequisite columns {list(prerequisites)}, found "
            f"{list(actual_prerequisites)}; missing fixed columns: "
            f"{sorted(missing_fixed)}. Use the matching config and HDF5 state file."
        )
    return prerequisites


def empty_state(prerequisites=None):
    """Create an empty processing state table with stable column types."""
    prerequisites = normalize_prerequisites(prerequisites)
    columns = {
        "start": pd.Series(dtype="datetime64[ns]"),
        "end": pd.Series(dtype="datetime64[ns]"),
        "mode": pd.Series(dtype="object"),
        "source": pd.Series(dtype="object"),
    }
    columns.update({name: pd.Series(dtype="bool") for name in prerequisites})
    columns.update(
        {
            "status": pd.Series(dtype="object"),
            "progress": pd.Series(dtype="float64"),
            "targets": pd.Series(dtype="object"),
            "job_id": pd.Series(dtype="object"),
            "attempts": pd.Series(dtype="int64"),
            "submitted_at": pd.Series(dtype="datetime64[ns]"),
            "updated_at": pd.Series(dtype="datetime64[ns]"),
            "message": pd.Series(dtype="object"),
        }
    )
    frame = pd.DataFrame(columns)
    frame.index = pd.Index([], dtype="int64", name="run_number")
    return frame


def normalize_state(frame, prerequisites=None):
    """Normalize dtypes before storing the state table."""
    frame = frame.copy()
    prerequisites = require_state_schema(frame, prerequisites)
    frame.index = frame.index.astype("int64")
    frame.index.name = "run_number"
    for column in ("start", "end", "submitted_at", "updated_at"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in prerequisites:
        frame[column] = frame[column].fillna(False).astype("bool")
    frame["progress"] = frame["progress"].fillna(0.0).astype("float64")
    frame["attempts"] = frame["attempts"].fillna(0).astype("int64")
    for column in ("mode", "source", "status", "targets", "job_id", "message"):
        frame[column] = frame[column].fillna("").astype(str)
    return frame.loc[:, list(state_columns(prerequisites))].sort_index()


def load_state(path, prerequisites=None):
    """Load the HDF5 state table, or return an empty typed table."""
    if not os.path.exists(path):
        return empty_state(prerequisites)
    frame = pd.read_hdf(path, key="runs")
    return normalize_state(frame, prerequisites)


def backup_path(path, index):
    return f"{os.path.abspath(path)}.backup-{index}"


def load_state_with_backup(path, backup_count=DEFAULT_BACKUP_COUNT, prerequisites=None):
    """Load state, restoring the newest readable backup if it was deleted."""
    if os.path.exists(path):
        return load_state(path, prerequisites)
    for index in range(1, backup_count + 1):
        candidate = backup_path(path, index)
        if not os.path.exists(candidate):
            continue
        try:
            frame = load_state(candidate, prerequisites)
        except StateSchemaError:
            raise
        except Exception as error:
            core.log.warning("Cannot read state backup %s: %s", candidate, error)
            continue
        save_state(path, frame, prerequisites)
        core.log.warning("Restored missing state file from %s", candidate)
        return frame
    return empty_state(prerequisites)


def save_state(path, frame, prerequisites=None):
    """Atomically replace the HDF5 state file."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp"
    if os.path.exists(temporary):
        os.remove(temporary)
    try:
        normalize_state(frame, prerequisites).to_hdf(
            temporary,
            key="runs",
            mode="w",
            format="table",
            data_columns=("status", "mode", "source"),
            min_itemsize={
                "mode": 64,
                "source": 32,
                "status": 32,
                "targets": 256,
                "job_id": 64,
                "message": 512,
            },
        )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def backup_state(path, backup_count=DEFAULT_BACKUP_COUNT):
    """Atomically rotate copies of the current HDF5 state file."""
    path = os.path.abspath(path)
    if backup_count <= 0 or not os.path.exists(path):
        return
    temporary = f"{path}.backup.tmp"
    if os.path.exists(temporary):
        os.remove(temporary)
    try:
        shutil.copy2(path, temporary)
        for index in range(backup_count, 1, -1):
            previous = backup_path(path, index - 1)
            current = backup_path(path, index)
            if os.path.exists(previous):
                os.replace(previous, current)
        os.replace(temporary, backup_path(path, 1))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def configured_prerequisites():
    return tuple(
        value.strip()
        for value in core.config["prerequisites"]["required_from_osg_dtypes"].split(",")
        if value.strip()
    )


def configured_excluded_sources():
    """Sources excluded from this reprocessing campaign."""
    return {
        value.strip().lower()
        for value in core.config["processing"].get("excluded_sources", "").split(",")
        if value.strip()
    }


def configured_run_modes():
    """RunDB modes selected for this processing campaign."""
    return tuple(
        value.strip()
        for value in core.config["context"].get("run_mode", "").split(",")
        if value.strip()
    )


def configured_detector():
    """Detector selected for this processing listener."""
    detector = core.config["context"].get("detector", "tpc").strip()
    allowed = ("tpc", "neutron_veto", "muon_veto")
    if detector not in allowed:
        raise ValueError(f"detector must be one of {allowed}, got {detector!r}")
    return detector


def minimum_run_number():
    value = core.config["context"].get("minimum_run_number", "None")
    return None if value == "None" else int(value)


def rucio_local_path():
    kwargs = core.configured_context_kwargs()
    path = kwargs.get("_rucio_local_path")
    if not path:
        raise ValueError("context_kwargs must define _rucio_local_path")
    return os.path.abspath(path)


def make_input_context(path):
    """Create a context that can only see the selected local Rucio mount."""
    st = core.get_context()
    st.storage = [straxen.RucioLocalFrontend(path=path)]
    return st


def latest_run(collection, detector):
    return collection.find_one(
        {"detectors": detector},
        {"number": 1, "start": 1, "end": 1, "mode": 1, "source": 1},
        sort=[("number", -1)],
    )


def recent_completed_runs(collection, minimum_run, lookback, detector, run_modes=()):
    query = {"end": {"$type": "date"}, "detectors": detector}
    if minimum_run is not None:
        query["number"] = {"$gt": minimum_run}
    if run_modes:
        query["mode"] = {"$in": list(run_modes)}
    projection = {
        "number": 1,
        "start": 1,
        "end": 1,
        "mode": 1,
        "source": 1,
        "tags": 1,
    }
    return list(collection.find(query, projection).sort("number", -1).limit(lookback))


def tag_names(document):
    return {tag.get("name") for tag in document.get("tags", []) if isinstance(tag, dict)}


def targets_for_run():
    """Use the configured targets for every run mode and source."""
    targets = [target.strip() for target in core.configured_targets() if target.strip()]
    return " ".join(targets)


def discover_runs(frame, documents):
    """Add newly completed RunDB documents to the state table."""
    now = utc_now()
    prerequisites = state_prerequisites(frame)
    columns = state_columns(prerequisites)
    for document in documents:
        number = int(document["number"])
        if number in frame.index or tag_names(document).intersection(EXCLUDED_TAGS):
            continue
        source = document.get("source") or ""
        frame.loc[number, list(columns)] = [
            pd.Timestamp(document.get("start")),
            pd.Timestamp(document.get("end")),
            document.get("mode") or "",
            source,
            *[False for _ in prerequisites],
            WAITING,
            0.0,
            targets_for_run(),
            "",
            0,
            pd.NaT,
            now,
            "Waiting for local Rucio prerequisites",
        ]
    frame.index = frame.index.astype("int64")
    frame.index.name = "run_number"
    return frame.sort_index()


def apply_exclusions(frame):
    """Keep excluded runs visible in HDF without scheduling them."""
    excluded_sources = configured_excluded_sources()
    now = utc_now()
    for number in frame.index[frame["status"].isin(PREREQUISITE_STATES)]:
        source = frame.at[number, "source"].strip().lower()
        mode = frame.at[number, "mode"].lower()
        is_kr = "kr-83m" in excluded_sources and "kr83m" in mode
        if source not in excluded_sources and not is_kr:
            continue
        excluded_source = "Kr-83m" if is_kr else source
        frame.at[number, "status"] = SKIPPED
        frame.at[number, "message"] = f"Excluded by SR3 processing policy: {excluded_source}"
        frame.at[number, "updated_at"] = now
    return frame


def update_prerequisites(frame, st, prerequisites):
    """Refresh local Rucio availability for runs not submitted yet."""
    prerequisites = require_state_schema(frame, prerequisites)
    now = utc_now()
    for number in frame.index[frame["status"].isin(PREREQUISITE_STATES)]:
        run_id = f"{int(number):06d}"
        try:
            available = {
                dtype: bool(st.is_stored(run_id, dtype))
                for dtype in prerequisites
            }
            for dtype in prerequisites:
                frame.at[number, dtype] = available[dtype]
            ready = all(available.values())
            frame.at[number, "status"] = READY if ready else WAITING
            missing = [dtype for dtype, stored in available.items() if not stored]
            frame.at[number, "message"] = (
                "Ready to submit"
                if ready
                else f"Waiting for local Rucio prerequisites: {', '.join(missing)}"
            )
        except Exception as error:
            frame.at[number, "status"] = WAITING
            frame.at[number, "message"] = f"{type(error).__name__}: {error}"
        frame.at[number, "updated_at"] = now
    return frame


def read_log(number, max_bytes=2_000_000):
    """Read the tail of a job log without loading an unbounded file."""
    path = core.log_fn.format(run_id=f"{int(number):06d}")
    if not os.path.exists(path):
        return "", path
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        content = f.read()
    if start:
        content = content.split(b"\n", 1)[-1]
    return content.decode(errors="replace"), path


def progress_from_log(text):
    values = [float(value) for value in PROGRESS_PATTERN.findall(text)]
    return max(values, default=0.0)


def log_has_completed(text):
    """Return whether straxer exited successfully and wrote its final marker."""
    return any(line.strip() == COMPLETION_MARKER for line in text.splitlines())


def log_has_error(text):
    ignore = core.config["processing"]["ignore_patterns_in_logs"].split(",")
    lines = [line for line in text.splitlines()[-100:] if all(p not in line for p in ignore)]
    ending = " ".join(lines).lower()
    return any(word in ending for word in ("traceback", "killed", "error", "exception"))


def completion_result(number, text):
    """Trust straxer's availability check and locate output by run number only."""
    if log_has_error(text):
        return None
    if ALREADY_AVAILABLE_MARKER in text:
        run_id = f"{int(number):06d}"
        # The listener and straxer may use different containers. Do not infer
        # targets or lineage from the listener's context or the state table.
        for folder_name, status in (("destination_folder", MOVED), ("base_folder", COMPLETED)):
            folder = core.config["context"][folder_name]
            paths = glob.iglob(os.path.join(glob.escape(folder), f"{run_id}-*"))
            if any(os.path.isdir(path) and not path.endswith("_temp") for path in paths):
                return status, f"Data already available; run output found in {folder_name}"
        return FAILED, (
            "Already-available log but no run output directories in base_folder/destination_folder"
        )
    if log_has_completed(text):
        return COMPLETED, "Processing job ended successfully"
    return None


def retry_failed_runs(frame):
    """Reset runs that were failed when this listener started."""
    now = utc_now()
    failed_runs = list(frame.index[frame["status"] == FAILED])
    for number in failed_runs:
        text, _ = read_log(number)
        result = completion_result(number, text)
        if result is not None and result[0] != FAILED:
            frame.at[number, "status"] = result[0]
            frame.at[number, "progress"] = 100.0
            frame.at[number, "message"] = result[1]
        else:
            frame.at[number, "status"] = WAITING
            frame.at[number, "progress"] = 0.0
            frame.at[number, "job_id"] = ""
            frame.at[number, "submitted_at"] = pd.NaT
            frame.at[number, "message"] = "Reset from failed for retry"
        frame.at[number, "updated_at"] = now
    core.log.info("Reset or recovered %d failed runs", len(failed_runs))
    return frame


def slurm_state(job_id):
    if not job_id:
        return None
    result = subprocess.run(
        ["squeue", "-h", "-j", str(job_id), "-o", "%T"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    states = result.stdout.split()
    return states[0].upper() if states else "LEFT_QUEUE"


def update_processing(frame):
    """Update submitted jobs from Slurm and straxer logs."""
    now = utc_now()
    monitored_states = ACTIVE_STATES + (FAILED,)
    for number in frame.index[frame["status"].isin(monitored_states)]:
        text, log_path = read_log(number)
        frame.at[number, "progress"] = progress_from_log(text)
        has_error = log_has_error(text)

        # Resolve already-available output before the generic completion
        # marker, which cannot distinguish local output from moved output.
        result = completion_result(number, text)
        if result is not None:
            frame.at[number, "status"] = result[0]
            if result[0] != FAILED:
                frame.at[number, "progress"] = 100.0
            frame.at[number, "message"] = result[1][:500]
            frame.at[number, "updated_at"] = now
            continue

        if frame.at[number, "status"] == FAILED:
            continue

        queue_state = slurm_state(frame.at[number, "job_id"])

        if queue_state in ("PENDING", "CONFIGURING", "SUSPENDED"):
            frame.at[number, "status"] = SUBMITTED
            frame.at[number, "message"] = f"Slurm state: {queue_state}"
        elif has_error:
            frame.at[number, "status"] = FAILED
            frame.at[number, "message"] = f"Error found in {log_path}"
        elif queue_state in ("RUNNING", "COMPLETING"):
            frame.at[number, "status"] = PROCESSING
            frame.at[number, "message"] = f"Slurm state: {queue_state}"
        elif queue_state == "LEFT_QUEUE":
            frame.at[number, "status"] = FAILED
            frame.at[number, "message"] = "Job left Slurm without a completion marker"
        elif text:
            frame.at[number, "status"] = PROCESSING
            frame.at[number, "message"] = "Job log has started; Slurm state is unknown"
        else:
            frame.at[number, "status"] = SUBMITTED
            frame.at[number, "message"] = f"Slurm state: {queue_state or 'unknown'}"
        frame.at[number, "updated_at"] = now
    return frame


def archive_old_log(number, attempt):
    path = core.log_fn.format(run_id=f"{int(number):06d}")
    if os.path.exists(path):
        os.replace(path, f"{path}.attempt-{attempt}.bak")


def extract_job_id(message):
    # utilix.batchq.submit_job returns the Slurm job ID or None.
    value = str(message).strip() if message is not None else ""
    return value if value.isdecimal() else ""


def build_job(number, targets):
    tag = core.config["processing"]["container_tag"]
    return submit_jobs._make_job(
        run_name=f"{int(number):06d}",
        targets=targets,
        base_folder=core.config["context"]["base_folder"],
        context=core.config["context"]["context"],
        package=core.config["context"]["package"],
        ram=int(core.config["processing"]["ram"]),
        cpus_per_task=int(core.config["processing"]["cpus_per_job"]),
        container=f"xenonnt-{tag}.simg",
        context_config_kwargs=core.configured_context_kwargs(),
    )


def submit_ready(frame, state_path, max_submit, prerequisites=None):
    """Submit ready runs and persist state after each successful submission."""
    partition = core.config["processing"]["allowed_partitions"].split(",")[0].strip()
    max_jobs = int(core.config["processing"]["max_jobs"])
    accounted_jobs = 0
    submitted_this_cycle = 0

    ready = list(frame.index[frame["status"] == READY])
    for number in ready:
        if max_submit and submitted_this_cycle >= max_submit:
            break

        # Recheck the user's whole Slurm queue before every submission. Keep a
        # local reservation for jobs just submitted in this cycle because
        # squeue may take a moment to show them.
        accounted_jobs = max(accounted_jobs, submit_jobs.n_jobs_running())
        if accounted_jobs >= max_jobs:
            core.log.info(
                "Not submitting run %06d: %d of %d allowed jobs are accounted for",
                int(number),
                accounted_jobs,
                max_jobs,
            )
            break

        attempt = int(frame.at[number, "attempts"]) + 1
        archive_old_log(number, attempt)
        job = build_job(number, frame.at[number, "targets"])

        # Persist this state before Slurm is called. If submission succeeds but
        # this process stops before recording the job ID, do not retry blindly.
        now = utc_now()
        frame.at[number, "status"] = SUBMITTING
        frame.at[number, "attempts"] = attempt
        frame.at[number, "updated_at"] = now
        frame.at[number, "message"] = "Submission may be in progress; check Slurm before retrying"
        save_state(state_path, frame, prerequisites)

        job.submit(partition=partition, qos=partition)

        now = utc_now()
        job_id = extract_job_id(job.submit_message)
        frame.at[number, "status"] = SUBMITTED if job_id else SUBMITTING
        frame.at[number, "job_id"] = job_id
        frame.at[number, "submitted_at"] = now
        frame.at[number, "updated_at"] = now
        frame.at[number, "message"] = (
            f"Submitted to {partition}"
            if job_id
            else "Submission returned without a job ID; check Slurm before retrying"
        )
        save_state(state_path, frame, prerequisites)
        accounted_jobs += 1
        submitted_this_cycle += 1
    return frame


def print_summary(frame, latest):
    if latest is not None:
        end = latest.get("end") or "still running"
        print(
            f"Latest RunDB run: {int(latest['number']):06d} | "
            f"mode={latest.get('mode', '')} | end={end}"
        )
    counts = frame["status"].value_counts().to_dict()
    summary = " | ".join(f"{state}={count}" for state, count in sorted(counts.items()))
    print(summary or "No runs in processing state")


def run_cycle(collection, input_context, frame, args):
    detector = configured_detector()
    latest = latest_run(collection, detector)
    documents = recent_completed_runs(
        collection,
        args.minimum_run,
        args.lookback,
        detector,
        configured_run_modes(),
    )
    frame = discover_runs(frame, documents)
    frame = apply_exclusions(frame)
    pending = frame["status"].isin(PREREQUISITE_STATES)
    frame.loc[pending, "targets"] = targets_for_run()
    frame = update_prerequisites(frame, input_context, args.prerequisites)
    frame = update_processing(frame)
    save_state(args.state_file, frame, args.prerequisites)
    if args.submit:
        frame = submit_ready(
            frame,
            args.state_file,
            args.max_submit_per_cycle,
            args.prerequisites,
        )
    print_summary(frame, latest)
    print(f"State file: {args.state_file}")
    return frame


def parse_args():
    base_folder = os.path.abspath(core.config["context"]["base_folder"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit ready runs. Without this flag the service only monitors state.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Once at startup, reset existing failed runs so they can be submitted again.",
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument(
        "--backup-every-cycles",
        type=int,
        default=10,
        help="Back up the state file after this many successful cycles; zero disables backups.",
    )
    parser.add_argument(
        "--backup-count",
        type=int,
        default=DEFAULT_BACKUP_COUNT,
        help="Number of rotating state-file backups to retain.",
    )
    parser.add_argument("--minimum-run", type=int, default=minimum_run_number())
    parser.add_argument(
        "--state-file",
        default=os.path.join(base_folder, "online_processing.h5"),
    )
    parser.add_argument(
        "--max-submit-per-cycle",
        type=int,
        default=1,
        help="Maximum submissions per cycle; zero uses all available queue capacity.",
    )
    parser.add_argument(
        "--rucio-path",
        default=rucio_local_path(),
    )
    parser.add_argument(
        "--prerequisites",
        nargs="+",
        default=configured_prerequisites(),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.poll_seconds <= 0 or args.lookback <= 0:
        raise ValueError("poll-seconds and lookback must be positive")
    if args.max_submit_per_cycle < 0:
        raise ValueError("max-submit-per-cycle cannot be negative")
    if args.backup_every_cycles < 0:
        raise ValueError("backup-every-cycles cannot be negative")
    if args.backup_count <= 0:
        raise ValueError("backup-count must be positive")
    args.prerequisites = normalize_prerequisites(args.prerequisites)
    if not os.path.isdir(args.rucio_path):
        raise FileNotFoundError(f"Local Rucio path not found: {args.rucio_path}")

    collection = xent_collection()
    input_context = make_input_context(args.rucio_path)
    retry_failed = args.retry_failed
    successful_cycles = 0

    while True:
        try:
            with state_lock(args.state_file):
                frame = load_state_with_backup(
                    args.state_file,
                    args.backup_count,
                    args.prerequisites,
                )
                if retry_failed:
                    frame = retry_failed_runs(frame)
                    save_state(args.state_file, frame, args.prerequisites)
                    retry_failed = False
                run_cycle(collection, input_context, frame, args)
                successful_cycles += 1
                if args.backup_every_cycles and (
                    not os.path.exists(backup_path(args.state_file, 1))
                    or successful_cycles % args.backup_every_cycles == 0
                ):
                    backup_state(args.state_file, args.backup_count)
        except StateSchemaError:
            core.log.exception("State-file schema mismatch; listener cannot continue")
            raise
        except Exception as error:
            core.log.exception("Online processing cycle failed: %s", error)
            if args.once:
                raise
        if args.once:
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
