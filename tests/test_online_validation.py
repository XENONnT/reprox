"""Offline regression tests for mixed SR3 output locations."""

import json
from unittest.mock import Mock

import pytest

from reprox import online_processing as online
from reprox import online_validation as validation


@pytest.fixture
def job(tmp_path):
    base = tmp_path / "base"
    nested = base / "strax_data"
    destination = tmp_path / "destination"
    nested.mkdir(parents=True)
    destination.mkdir()
    state = tmp_path / "state.h5"
    frame = online.discover_runs(online.empty_state(), [{"number": 123}, {"number": 124}])
    frame["status"] = online.COMPLETED
    return frame, state, base, nested, destination


def output(folder, number=123, dtype="event_info", broken=False):
    path = folder / f"{number:06d}-{dtype}-testhash"
    path.mkdir()
    metadata = {"chunks": [{"n": 1, "filename": "chunk000000"}]}
    (path / "metadata.json").write_text(json.dumps(metadata))
    if not broken:
        (path / "chunk000000").write_bytes(b"test chunk")
    return path


@pytest.mark.parametrize("location", ["base", "nested", "both"])
def test_validate_and_move_all_source_locations(job, location):
    frame, state, base, nested, destination = job
    paths = []
    if location in ("base", "both"):
        paths.append(output(base))
    if location in ("nested", "both"):
        paths.append(output(nested, dtype="event_basics"))
    validation.validate_and_move_run(frame, state, 123, base, destination, None)
    assert online.load_state(state).at[123, "status"] == online.MOVED
    for path in paths:
        assert not path.exists()
        assert (destination / path.name / "chunk000000").read_bytes() == b"test chunk"


@pytest.mark.parametrize("conflict", ["source", "destination"])
def test_collisions_leave_all_output_in_place(job, conflict):
    frame, state, base, nested, destination = job
    original = output(base)
    duplicate = output(nested if conflict == "source" else destination)
    other = output(nested, dtype="event_basics")
    validation.validate_and_move_run(frame, state, 123, base, destination, None)
    assert frame.at[123, "status"] == online.VALIDATION_FAILED
    assert all(path.exists() for path in (original, duplicate, other))
    assert not (destination / other.name).exists()


def test_bad_nested_output_prevents_partial_move(job):
    frame, state, base, nested, destination = job
    good = output(base)
    bad = output(nested, dtype="event_basics", broken=True)
    validation.validate_and_move_run(frame, state, 123, base, destination, None)
    assert frame.at[123, "status"] == online.VALIDATION_FAILED
    assert "misses_chunks" in frame.at[123, "message"]
    assert good.exists() and bad.exists()
    assert not list(destination.iterdir())


@pytest.mark.parametrize("location", ["base", "nested", "destination"])
def test_missing_output_failure_recovers_when_output_appears(job, location):
    frame, state, base, nested, destination = job
    frame.at[123, "status"] = online.VALIDATION_FAILED
    frame.at[123, "message"] = validation.MISSING_OUTPUT_MESSAGE
    path = output({"base": base, "nested": nested, "destination": destination}[location])
    online.save_state(state, frame)
    validation.run_cycle(state, base, destination, None, None, 1)
    saved = online.load_state(state)
    assert saved.at[123, "status"] == online.MOVED
    assert saved.at[124, "status"] == online.COMPLETED
    assert (destination / path.name).exists()


def test_missing_run_does_not_block_later_run(job):
    frame, state, base, nested, destination = job
    output(nested, number=124)
    online.save_state(state, frame)
    validation.run_cycle(state, base, destination, None, None, 1)
    assert online.load_state(state).at[123, "status"] == online.VALIDATION_FAILED
    validation.run_cycle(state, base, destination, None, None, 1)
    saved = online.load_state(state)
    assert saved.at[123, "status"] == online.VALIDATION_FAILED
    assert saved.at[124, "status"] == online.MOVED


def test_nested_filesystem_checked_before_any_move(job, monkeypatch):
    frame, state, base, nested, destination = job
    original = output(base)
    other = output(nested, dtype="event_basics")
    check = Mock(side_effect=[None, RuntimeError("different filesystems")])
    monkeypatch.setattr(validation, "require_same_filesystem", check)
    with pytest.raises(RuntimeError, match="different filesystems"):
        validation.validate_and_move_run(frame, state, 123, base, destination, None)
    assert check.call_count == 2
    assert original.exists() and other.exists()
    assert not list(destination.iterdir())


def test_already_partly_moved_run_finishes(job):
    frame, state, base, nested, destination = job
    frame.at[123, "status"] = online.MOVING
    previous = output(destination)
    remaining = output(nested, dtype="event_basics")
    validation.validate_and_move_run(frame, state, 123, base, destination, None)
    assert frame.at[123, "status"] == online.MOVED
    assert previous.exists() and (destination / remaining.name).exists()
