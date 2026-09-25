"""Offline regression coverage for already-available straxer jobs."""

from unittest.mock import Mock

import pytest

from reprox import core, online_processing as online


@pytest.fixture
def job(tmp_path, monkeypatch):
    base = tmp_path / "base"
    destination = tmp_path / "destination"
    base.mkdir()
    destination.mkdir()
    monkeypatch.setitem(core.config["context"], "base_folder", str(base))
    monkeypatch.setitem(core.config["context"], "destination_folder", str(destination))
    monkeypatch.setitem(core.config["processing"], "ignore_patterns_in_logs", "UserWarning")
    monkeypatch.setattr(core, "get_context", Mock(side_effect=AssertionError("No context lookup")))
    frame = online.discover_runs(online.empty_state(), [{"number": 123}])
    frame.at[123, "targets"] = "event_info"
    frame.at[123, "status"] = online.SUBMITTED
    frame.at[123, "job_id"] = "456"
    queue = Mock(return_value="LEFT_QUEUE")
    monkeypatch.setattr(online, "slurm_state", queue)
    return frame, base, destination, queue


def write_output(folder, name="000123-event_info-containerhash"):
    path = folder / name
    path.mkdir()
    return path


def set_log(monkeypatch, text):
    monkeypatch.setattr(online, "read_log", lambda number: (text, "slurmlog.txt"))


@pytest.mark.parametrize("initial", [online.SUBMITTED, online.PROCESSING, online.FAILED])
@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("location, expected", [
    ("base", online.COMPLETED),
    ("destination", online.MOVED),
    ("both", online.MOVED),
])
def test_already_available_location(job, monkeypatch, tmp_path, initial, marker, location, expected):
    frame, base, destination, queue = job
    frame.at[123, "status"] = initial
    if location in ("base", "both"):
        write_output(base)
    if location in ("destination", "both"):
        write_output(destination)
    text = "INFO: " + online.ALREADY_AVAILABLE_MARKER + "\n"
    if marker:
        text += online.COMPLETION_MARKER + "\n"
    set_log(monkeypatch, text)

    online.update_processing(frame)
    state_path = tmp_path / "state.h5"
    online.save_state(state_path, frame)
    saved = online.load_state(state_path)
    assert saved.at[123, "status"] == expected
    assert saved.at[123, "progress"] == 100.0
    assert saved.at[123, "job_id"] == "456"
    queue.assert_not_called()


@pytest.mark.parametrize("location, expected", [
    ("base", online.COMPLETED), ("destination", online.MOVED),
])
def test_retry_recovers_available_output(job, monkeypatch, location, expected):
    frame, base, destination, _ = job
    frame.at[123, "status"] = online.FAILED
    write_output(base if location == "base" else destination)
    set_log(monkeypatch, online.ALREADY_AVAILABLE_MARKER)
    online.retry_failed_runs(frame)
    assert frame.at[123, "status"] == expected
    assert frame.at[123, "progress"] == 100.0


@pytest.mark.parametrize("invalid", ["missing", "temporary", "file", "different_run"])
def test_missing_run_output_cannot_use_generic_completion(job, monkeypatch, invalid):
    frame, base, _, _ = job
    if invalid == "temporary":
        write_output(base, "000123-event_info-containerhash_temp")
    elif invalid == "file":
        (base / "000123-event_info-containerhash").touch()
    elif invalid == "different_run":
        write_output(base, "0001234-event_info-containerhash")
    set_log(monkeypatch, online.ALREADY_AVAILABLE_MARKER + "\n" + online.COMPLETION_MARKER)
    online.update_processing(frame)
    assert frame.at[123, "status"] == online.FAILED
    assert "no run output directories" in frame.at[123, "message"]


@pytest.mark.parametrize("targets", ["event_info event_info_double", "different_target", ""])
def test_targets_lineage_and_metadata_are_not_checked(job, monkeypatch, targets):
    frame, _, destination, _ = job
    frame.at[123, "targets"] = targets
    write_output(destination, "000123-container_output-differenthash")
    set_log(monkeypatch, online.ALREADY_AVAILABLE_MARKER)
    online.update_processing(frame)
    assert frame.at[123, "status"] == online.MOVED
    core.get_context.assert_not_called()


def test_errors_override_available_marker(job, monkeypatch):
    frame, _, destination, _ = job
    write_output(destination)
    set_log(monkeypatch, online.ALREADY_AVAILABLE_MARKER + "\nTraceback: job failed\n")
    online.update_processing(frame)
    assert frame.at[123, "status"] == online.FAILED


def test_normal_completion_needs_no_storage_lookup(job, monkeypatch):
    frame, _, _, _ = job
    set_log(monkeypatch, online.COMPLETION_MARKER)
    online.update_processing(frame)
    assert frame.at[123, "status"] == online.COMPLETED
    core.get_context.assert_not_called()
