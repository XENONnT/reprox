"""State-file failure cases must preserve the current state and good backups."""

from pathlib import Path

import pandas as pd
import pytest

from reprox import online_processing as online


@pytest.mark.parametrize("prerequisites", [("peaklets", "lone_hits"), ("raw_records_nv",)])
@pytest.mark.parametrize("empty", [False, True])
def test_state_roundtrip_including_empty_and_nv(tmp_path, prerequisites, empty):
    frame = online.empty_state(prerequisites)
    if not empty:
        frame = online.discover_runs(frame, [{"number": 123}])
    path = tmp_path / "state.h5"
    online.save_state(path, frame, prerequisites)
    pd.testing.assert_frame_equal(online.load_state(path, prerequisites),
                                  online.normalize_state(frame, prerequisites))
    online.backup_state(path)
    pd.testing.assert_frame_equal(pd.read_hdf(str(path) + ".backup-1", key="runs"),
                                  pd.read_hdf(path, key="runs"))


@pytest.mark.parametrize("damage", ["empty", "wrong_key", "changed_values"])
def test_failed_write_preserves_previous_state(tmp_path, monkeypatch, damage):
    frame = online.discover_runs(online.empty_state(), [{"number": 123}])
    path = tmp_path / "state.h5"
    online.save_state(path, frame)
    previous = path.read_bytes()
    original_write = pd.DataFrame.to_hdf

    def damaged_write(self, target, **kwargs):
        if damage == "empty":
            Path(target).write_bytes(b"")
        elif damage == "wrong_key":
            original_write(self, target, **dict(kwargs, key="wrong"))
        else:
            wrong = self.copy()
            wrong.at[123, "attempts"] += 1
            original_write(wrong, target, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_hdf", damaged_write)
    with pytest.raises(RuntimeError, match="Refusing invalid state file"):
        online.save_state(path, frame)
    assert path.read_bytes() == previous
    assert not Path(str(path) + ".tmp").exists()


@pytest.mark.parametrize("contents", [b"", b"not an HDF5 file"])
def test_corrupt_main_does_not_rotate_good_backups(tmp_path, contents):
    frame = online.discover_runs(online.empty_state(), [{"number": 123}])
    path = tmp_path / "state.h5"
    online.save_state(path, frame)
    for _ in range(3):
        online.backup_state(path)
    before = {i: Path(online.backup_path(path, i)).read_bytes() for i in range(1, 4)}
    path.write_bytes(contents)
    with pytest.raises(RuntimeError, match="Refusing invalid state file"):
        online.backup_state(path)
    for i in range(1, 4):
        assert Path(online.backup_path(path, i)).read_bytes() == before[i]
    assert not Path(str(path) + ".backup.tmp").exists()


@pytest.mark.parametrize("failure", ["interrupt", "flush"])
def test_interruption_or_flush_error_preserves_previous_state(tmp_path, monkeypatch, failure):
    frame = online.discover_runs(online.empty_state(), [{"number": 123}])
    path = tmp_path / "state.h5"
    online.save_state(path, frame)
    previous = path.read_bytes()
    if failure == "interrupt":
        def interrupted_write(self, target, **kwargs):
            Path(target).write_bytes(b"")
            raise KeyboardInterrupt()
        monkeypatch.setattr(pd.DataFrame, "to_hdf", interrupted_write)
        expected_error = KeyboardInterrupt
    else:
        def failed_flush(handle):
            raise OSError("simulated storage flush failure")
        monkeypatch.setattr(online.os, "fsync", failed_flush)
        expected_error = OSError
    with pytest.raises(expected_error):
        online.save_state(path, frame)
    assert path.read_bytes() == previous
    assert not Path(str(path) + ".tmp").exists()
