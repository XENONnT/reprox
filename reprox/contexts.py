"""Contexts used by reprox utility jobs."""

import os

import cutax


def xenonnt_online_recompute(
        output_folder=None,
        input_only=("peaklets", "lone_hits"),
        **kwargs):
    """Build an online context that recomputes everything above input_only."""
    if output_folder is None:
        output_folder = os.environ.get("REPROX_OUTPUT_FOLDER")
    if output_folder is None:
        # Keep jobs made by older versions of submit_profile.py working.
        output_folder = os.environ.get("REPROX_PROFILE_OUTPUT")
    if not output_folder:
        raise ValueError("output_folder or REPROX_OUTPUT_FOLDER is required")
    st = cutax.contexts.xenonnt_online(output_folder=output_folder, **kwargs)
    output_folder = os.path.realpath(output_folder)
    found_output = False

    for storage in st.storage:
        storage_path = getattr(storage, "path", None)
        if storage_path is not None and os.path.realpath(storage_path) == output_folder:
            found_output = True
            continue
        storage.take_only = tuple(input_only)

    if not found_output:
        raise RuntimeError(f"No writable DataDirectory found at {output_folder}")
    return st


def xenonnt_online_profile(
        output_folder=None,
        input_only=("peaklets", "lone_hits"),
        **kwargs):
    """Backward-compatible name for the profiling submission script."""
    return xenonnt_online_recompute(
        output_folder=output_folder,
        input_only=input_only,
        **kwargs,
    )
