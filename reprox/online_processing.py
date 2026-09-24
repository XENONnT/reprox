"""RunDB monitoring and state-driven online reprocessing."""

import argparse
from contextlib import contextmanager
import fcntl
import os
import re
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

STATE_COLUMNS = (
    "start",
    "end",
    "mode",
    "source",
    "peaklets",
    "lone_hits",
    "status",
    "progress",
    "targets",
    "job_id",
    "attempts",
    "submitted_at",
    "updated_at",
    "message",
)


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


def empty_state():
    """Create an empty processing state table with stable column types."""
    frame = pd.DataFrame(
        {
            "start": pd.Series(dtype="datetime64[ns]"),
            "end": pd.Series(dtype="datetime64[ns]"),
            "mode": pd.Series(dtype="object"),
            "source": pd.Series(dtype="object"),
            "peaklets": pd.Series(dtype="bool"),
            "lone_hits": pd.Series(dtype="bool"),
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
    frame.index = pd.Index([], dtype="int64", name="run_number")
    return frame


def normalize_state(frame):
    """Normalize dtypes before storing the state table."""
    frame = frame.copy()
    frame.index = frame.index.astype("int64")
    frame.index.name = "run_number"
    for column in ("start", "end", "submitted_at", "updated_at"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("peaklets", "lone_hits"):
        frame[column] = frame[column].fillna(False).astype("bool")
    frame["progress"] = frame["progress"].fillna(0.0).astype("float64")
    frame["attempts"] = frame["attempts"].fillna(0).astype("int64")
    for column in ("mode", "source", "status", "targets", "job_id", "message"):
        frame[column] = frame[column].fillna("").astype(str)
    return frame.loc[:, list(STATE_COLUMNS)].sort_index()


def load_state(path):
    """Load the HDF5 state table, or return an empty typed table."""
    if not os.path.exists(path):
        return empty_state()
    frame = pd.read_hdf(path, key="runs")
    missing = set(STATE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"State file is missing columns: {sorted(missing)}")
    return normalize_state(frame)


def save_state(path, frame):
    """Atomically replace the HDF5 state file."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp"
    if os.path.exists(temporary):
        os.remove(temporary)
    try:
        normalize_state(frame).to_hdf(
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


def latest_run(collection):
    return collection.find_one(
        {},
        {"number": 1, "start": 1, "end": 1, "mode": 1, "source": 1},
        sort=[("number", -1)],
    )


def recent_completed_runs(collection, minimum_run, lookback, run_modes=()):
    query = {"end": {"$type": "date"}, "detectors": "tpc"}
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
    for document in documents:
        number = int(document["number"])
        if number in frame.index or tag_names(document).intersection(EXCLUDED_TAGS):
            continue
        source = document.get("source") or ""
        frame.loc[number, list(STATE_COLUMNS)] = [
            pd.Timestamp(document.get("start")),
            pd.Timestamp(document.get("end")),
            document.get("mode") or "",
            source,
            False,
            False,
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
    now = utc_now()
    for number in frame.index[frame["status"].isin(PREREQUISITE_STATES)]:
        run_id = f"{int(number):06d}"
        try:
            available = {
                dtype: bool(st.is_stored(run_id, dtype))
                for dtype in prerequisites
            }
            for dtype in ("peaklets", "lone_hits"):
                frame.at[number, dtype] = available.get(dtype, False)
            ready = all(available.values())
            frame.at[number, "status"] = READY if ready else WAITING
            frame.at[number, "message"] = (
                "Ready to submit" if ready else "Waiting for local Rucio prerequisites"
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

        # New job scripts only write this marker when straxer exits with zero.
        # Requiring an error-free log also keeps older unconditional markers
        # from turning known failures into completed runs.
        if log_has_completed(text) and not has_error:
            frame.at[number, "status"] = COMPLETED
            frame.at[number, "progress"] = 100.0
            frame.at[number, "message"] = "Processing job ended successfully"
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


def submit_ready(frame, state_path, max_submit):
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
        save_state(state_path, frame)

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
        save_state(state_path, frame)
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
    latest = latest_run(collection)
    documents = recent_completed_runs(
        collection,
        args.minimum_run,
        args.lookback,
        configured_run_modes(),
    )
    frame = discover_runs(frame, documents)
    frame = apply_exclusions(frame)
    pending = frame["status"].isin(PREREQUISITE_STATES)
    frame.loc[pending, "targets"] = targets_for_run()
    frame = update_prerequisites(frame, input_context, args.prerequisites)
    frame = update_processing(frame)
    save_state(args.state_file, frame)
    if args.submit:
        frame = submit_ready(frame, args.state_file, args.max_submit_per_cycle)
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
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--lookback", type=int, default=100)
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
    if not args.prerequisites:
        raise ValueError("At least one prerequisite is required")
    if not os.path.isdir(args.rucio_path):
        raise FileNotFoundError(f"Local Rucio path not found: {args.rucio_path}")

    collection = xent_collection()
    input_context = make_input_context(args.rucio_path)

    while True:
        try:
            with state_lock(args.state_file):
                frame = load_state(args.state_file)
                run_cycle(collection, input_context, frame, args)
        except Exception as error:
            core.log.exception("Online processing cycle failed: %s", error)
            if args.once:
                raise
        if args.once:
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
