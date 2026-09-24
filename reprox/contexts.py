"""Contexts used only by isolated reprox test jobs."""

import os

import cutax


def xenonnt_online(
        output_folder=None,
        take_only=("peaklets", "lone_hits"),
        **kwargs):
    """Build a test context that can read only the selected input data types."""
    if output_folder is None:
        output_folder = os.environ.get("REPROX_PROFILE_OUTPUT")
    if not output_folder:
        raise ValueError("output_folder or REPROX_PROFILE_OUTPUT is required")
    st = cutax.contexts.xenonnt_online(output_folder=output_folder, **kwargs)
    output_folder = os.path.realpath(output_folder)
    found_output = False

    for storage in st.storage:
        storage_path = getattr(storage, "path", None)
        if storage_path is not None and os.path.realpath(storage_path) == output_folder:
            found_output = True
            continue
        storage.take_only = tuple(take_only)

    if not found_output:
        raise RuntimeError(f"No writable DataDirectory found at {output_folder}")
    return st
