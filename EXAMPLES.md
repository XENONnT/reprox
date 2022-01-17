# Reprocessing on dali

Process data in so far available on dali with the current container

## Logic

There are several (sequential) steps with (associated scripts):

- Step 1. Find runs to process (`reprox-find-data`)
- Step 2. Process the runs that were found (`reprox-start-jobs`)
- Step 3. Move the data that was processed to the desired folder (`reprox-move-folders`)

One can also run these three steps from one file (`reprox-reprocess`), which runs all three in
order.

The best place to start is by going over these files and do
`reprox-find-data --help` to see which options there are. Most are discussed below.

## Running step by step
Below, we show how these three steps are done. This can also be done in one 
command [skip to single command.](https://reprox.readthedocs.io/en/latest/reference/examples.html#run-entire-workflow-(steps-1-3-in-a-single-command)

### Step 0 - Activation and test installation
You only have to do it once, to prevent confusion we will go over it step by step.

First, activate a container (NB! the singularity containers do not work 
as they cannot communicate with the job submission of dali).

```bash
source /cvmfs/xenon.opensciencegrid.org/releases/nT/development/setup.sh
git clone git@github.com:XENONnT/reprox.git
pip install -e reprox --user
```
test that the installation is complete and successful
```bash
reprox-find-data --help
```

#### Trouble-shooting
Now, the commands above may sometimes not work as expected due to permission errors on the 
containers. If there is an error, you could see ``reprox-find-data: command not found``.
If this is the case, simply navigate to the `bin` folder of `reprox` and 
run the commands as below:

```bash 
cd repox/bin
python reprox-find-data --help
```

The other `reprox` scripts are similarly located in the `bin` folder. If you had
to change this once, you have to do `python <script>` for all the scripts 
listed below.

### Step 1 - finding data to (re)process on dali
Now we have to know which data to process, this can be done with the following 
command. Determine which data to process:

```bash
reprox-find-data \
    --package cutax \
    --context xenonnt_v6 \ 
    --target event_info event_pattern_fit cuts_basic \
    --cmt-version global_v6
```
The `--package` and `--context` arguments specify where to load the context 
from (`straxen`/`cutax`) and which context to use. In this example, we use `xenonnt_v6`.
The `--target ` argument specifies which datatypes to produce. This can be a 
list as in the example above. We will check if the datatypes can be produced for this given context. 
Since some context may use a global CMT version that is only valid for a range of runs,
the `--cmt-version` is specified separately and tells the script to only process runs 
that are valid in this `cmt_version`. This can be disabled using `--cmt-version False` 
(for example, you know that the CMT version is always valid for the datatypes you requested).

This takes a while (+/- 30 minutes) and writes a file
called `/dali/lgrandi/xenonnt/data_management_reprocessing/to_do_runs.csv` (depending on your ini
file). This file has a list of runs that you can process given the options as above.


### Step 2 - starting the jobs to process the data
After producing `/dali/lgrandi/xenonnt/data_management_reprocessing/to_do_runs.csv`, we need to 
submit the jobs to process the data. Most of the arguments are the same as above,
we now also specify some self-explanatory arguments for the jobs to be submitted. 
```bash
reprox-start-jobs \
    --package cutax \
    --context xenonnt_v6 \
    --target event_info event_pattern_fit cuts_basic \
    --ram 12000 \
    --cpu 2
```

### Step 3 - move to the production folder
Now, hopefully most of the data has been processed successfully, we can now move it to the
production folder. This includes a check to see if the data was processed successfully so
even if a few jobs failed (or are still running), you can safely run this command below.

```bash
reprox-move-folders
```

### Run entire workflow (steps 1-3 in a single command) 
You can also do all the above in a single command, using the same arguments (see above for explanation of each.).

```bash
reprox-reprocess \
    --package cutax \
    --context xenonnt_v6 \
    --target event_info event_pattern_fit cuts_basic \
    --cmt-version global_v6 \
    --ram 12000 \
    --cpu 2 \
    --move-after-workflow # To move the data into the production folder
```

# Advanced usage

Below are several more advanced use cases.

## Changing the defaults of processing

You might want to play with the config file that says how many resources one uses by default.
The [reprocessing.ini](https://github.com/XENONnT/reprox/blob/master/reprox/reprocessing.ini)
file. You can either change the source code of this file, or you can overwrite it as follows:

```bash
git clone git@github.com:XENONnT/reprox.git
cp reprox/reprox/reprocessing.ini my_reprocessing_config.ini

# # Edit my_reprocessing_config.ini. For example using vim:
# vi my_reprocessing_config.ini 

# overwrite the file used using an environment variable
export REPROX_CONFIG=$(pwd)/my_reprocessing_config.ini
```

You will see that your defaults have been changed (e.g. do `reprox-reprocess --help`) reflecting the
changes you made in the `.ini` file.

## Use custom config

You might want to process some data with slightly different settings, this can be done using
the`--context_kwargs` argument as follows
(please don't move it into the production folder unless you know what you are doing):

```bash
reprox-reprocess \
    --package cutax \
    --context xenonnt_v6 \
    --target event_info event_pattern_fit cuts_basic \
    --cmt-version global_v6 \
    --ram 12000 \
    --cpu 2 \ 
    --context-kwargs '{"s1_min_coincidence": 2, "s2_min_pmts": 10}'
```

## Using `reprox` from your jupyter notebook

You can also run the commands from above in a notebook or python script.

```python
from reprox import find_data, submit_jobs, validate_run

targets = 'event_info event_pattern_fit cuts_basic'.split()

# First determine which data to process
find_data.find_data(
    targets=targets,
    exclude_from_invalid_cmt_version='global_v6'
)
# Now start running the jobs
submit_jobs.submit_jobs(targets=targets)

# Finally move the jobs to the production folder
validate_run.move_all()
```

## Processing NV data

By default, the package assumes that only linked-mode or TPC runs are processed, if you want to
instead process NV data you need to tell the scripts to also take into account the NV detector:

```bash
reprox-reprocess \
    --package cutax \
    --context xenonnt_v6 \
    --target events_nv \
    --detectors neutron_veto muon_veto
    --ram 12000 \
    --cpu 2 \ 
    --cmt-version False
```

## Using tagged versions

One might want to run with a different tag as so

```bash
MY_TAG=2021.12.2
source /cvmfs/xenon.opensciencegrid.org/releases/nT/$MY_TAG/setup.sh
reprox-reprocess \
    --package cutax  \
    --context xenonnt_v5  \
    --targets event_info \
    --cmt-version global_v5 \
    --ram 24000  \
    --cpu 2  \
    --move-after-workflow \
    --tag $MY_TAG
```


