def test_find_runs():
    from reprox import find_runs
    find_runs.determine_data(
        targets='event_info_double',
        exclude_from_invalid_cmt_version='global_v6',
    )