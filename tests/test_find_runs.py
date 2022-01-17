def test_find_runs():
    from reprox import find_runs
    find_runs.determine_data(
        targets=targets,
        exclude_from_invalid_cmt_version=exclude_from_invalid_cmt_version,
        context_kwargs=context_kwargs,
        keep_detectors=args.detectors,
    )