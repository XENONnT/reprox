"""Shallow-validate completed online runs, move them, and update state."""

import argparse
import glob
import os
import time
from collections import Counter

from reprox import core, validate_run
from reprox import online_processing


RETRYABLE_STATES = (
    online_processing.COMPLETED,
    online_processing.VALIDATING,
    online_processing.MOVING,
)
MISSING_OUTPUT_MESSAGE = "No output directories found in source or destination"


def configured_paths():
    # The state file stays in base_folder; output may also be in strax_data.
    source = os.path.abspath(core.config["context"]["base_folder"])
    destination = os.path.abspath(core.config["context"]["destination_folder"])
    return source, destination


def require_same_filesystem(source, destination):
    """Refuse moves that would copy data between filesystems."""
    if not os.path.isdir(source):
        raise FileNotFoundError(f"Processing output folder not found: {source}")
    os.makedirs(destination, exist_ok=True)
    source_device = os.stat(source).st_dev
    destination_device = os.stat(destination).st_dev
    if source_device != destination_device:
        raise RuntimeError(
            "Refusing to move between different filesystems: "
            f"{source} (device {source_device}) -> "
            f"{destination} (device {destination_device})"
        )


def run_folders(folder, number):
    run_id = f"{int(number):06d}"
    return sorted(
        path
        for path in glob.glob(os.path.join(folder, f"{run_id}-*"))
        if os.path.isdir(path)
    )


def source_run_folders(source, number):
    """Find both reprox output and cutax's default ./strax_data output."""
    paths = run_folders(source, number) + run_folders(
        os.path.join(source, "strax_data"), number
    )
    # Do not process the same directory twice if a source path is a symlink.
    return sorted({os.path.realpath(path): path for path in paths}.values())


def set_status(frame, state_path, number, status, message):
    frame.at[number, "status"] = status
    frame.at[number, "message"] = message[:500]
    frame.at[number, "updated_at"] = online_processing.utc_now()
    online_processing.save_state(state_path, frame)


def validate_and_move_run(frame, state_path, number, source, destination, group):
    """Shallow-validate and move every output directory for one run."""
    source_folders = source_run_folders(source, number)
    destination_folders = run_folders(destination, number)

    # Recover cleanly if movement finished before the HDF state was updated.
    if not source_folders:
        if destination_folders:
            set_status(
                frame,
                state_path,
                number,
                online_processing.MOVED,
                f"Already moved {len(destination_folders)} output directories",
            )
        else:
            set_status(
                frame,
                state_path,
                number,
                online_processing.VALIDATION_FAILED,
                MISSING_OUTPUT_MESSAGE,
            )
        return

    duplicate_names = sorted(
        name for name, count in Counter(
            os.path.basename(path) for path in source_folders
        ).items() if count > 1
    )
    if duplicate_names:
        set_status(
            frame,
            state_path,
            number,
            online_processing.VALIDATION_FAILED,
            "Duplicate output directories across sources: " + ", ".join(duplicate_names),
        )
        return

    collisions = [
        path for path in source_folders
        if os.path.exists(os.path.join(destination, os.path.basename(path)))
    ]
    if collisions:
        names = ", ".join(os.path.basename(path) for path in collisions)
        set_status(
            frame,
            state_path,
            number,
            online_processing.VALIDATION_FAILED,
            f"Destination already contains: {names}",
        )
        return

    set_status(
        frame,
        state_path,
        number,
        online_processing.VALIDATING,
        f"Shallow-validating {len(source_folders)} output directories",
    )
    failures = []
    for path in source_folders:
        error = validate_run.RunValidation(
            path,
            mode=validate_run.ValidationLevel.SHALLOW,
        ).find_error()
        if error:
            failures.append(f"{os.path.basename(path)}: {error}")

    if failures:
        set_status(
            frame,
            state_path,
            number,
            online_processing.VALIDATION_FAILED,
            "; ".join(failures),
        )
        return

    # A nested output directory can itself be a mount or a symlink.
    # Check every directory before moving any of this run's output.
    for path in source_folders:
        require_same_filesystem(path, destination)

    set_status(
        frame,
        state_path,
        number,
        online_processing.MOVING,
        f"Moving {len(source_folders)} validated output directories",
    )
    try:
        for path in source_folders:
            error = validate_run.move_folder(
                path,
                destination_folder=destination,
                group=group,
                validation_level=validate_run.ValidationLevel.SHALLOW,
            )
            if error:
                raise RuntimeError(f"{os.path.basename(path)}: {error}")
    except Exception as error:
        set_status(
            frame,
            state_path,
            number,
            online_processing.MOVING,
            f"Move interrupted: {type(error).__name__}: {error}",
        )
        raise

    moved_folders = run_folders(destination, number)
    set_status(
        frame,
        state_path,
        number,
        online_processing.MOVED,
        f"Shallow validation passed; moved {len(moved_folders)} output directories",
    )


def run_cycle(state_path, source, destination, group, run_number, max_runs):
    with online_processing.state_lock(state_path):
        frame = online_processing.load_state_with_backup(state_path)
        retry_wrong_path = (
            frame["status"].eq(online_processing.VALIDATION_FAILED)
            & frame["message"].eq(MISSING_OUTPUT_MESSAGE)
        )
        candidates = frame.index[
            frame["status"].isin(RETRYABLE_STATES) | retry_wrong_path
        ]
        if run_number is not None:
            candidates = candidates[candidates == run_number]
        # Retry missing-output failures only once output appears, so an absent
        # run cannot consume every cycle when max_runs is one.
        candidates = [
            number for number in candidates
            if not retry_wrong_path.at[number]
            or source_run_folders(source, number)
            or run_folders(destination, number)
        ]
        if max_runs:
            candidates = candidates[:max_runs]

        for number in candidates:
            try:
                validate_and_move_run(
                    frame,
                    state_path,
                    number,
                    source,
                    destination,
                    group,
                )
            except Exception:
                core.log.exception("Validation/move failed for run %06d", int(number))

        online_processing.print_summary(frame, latest=None)
        print(f"State file: {state_path}")
        online_processing.backup_state(state_path)


def parse_args():
    base_folder = os.path.abspath(core.config["context"]["base_folder"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--run", type=int, help="Only process this run number.")
    parser.add_argument(
        "--max-runs-per-cycle",
        type=int,
        default=1,
        help="Maximum runs moved per cycle; zero processes all completed runs.",
    )
    parser.add_argument(
        "--state-file",
        default=os.path.join(base_folder, "online_processing.h5"),
    )
    parser.add_argument(
        "--group",
        default=None,
        help="Optional group override; by default move preserves ownership and permissions.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.poll_seconds <= 0:
        raise ValueError("poll-seconds must be positive")
    if args.max_runs_per_cycle < 0:
        raise ValueError("max-runs-per-cycle cannot be negative")

    source, destination = configured_paths()
    require_same_filesystem(source, destination)

    while True:
        run_cycle(
            args.state_file,
            source,
            destination,
            args.group,
            args.run,
            args.max_runs_per_cycle,
        )
        if args.once:
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
