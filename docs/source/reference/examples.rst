Reprocessing on dali
====================

Process data in so far available on dali with the current container.

Important notice: this package is meant for processing high level data
(greater than ``peaklets``) as it relies on readily available data on dali.


Logic
-----

There are several (sequential) steps with (associated scripts):


* Find runs to process (\ ``reprox-find-data``\ )
* Process the runs that were found (\ ``reprox-start-jobs``\ )
* Move the data that was processed to the desired folder (\ ``reprox-move-folders``\ )

One can also run these three steps from one file (\ ``reprox-start-jobs``\ ), which runs all three in
order.

The best place to start is by going over these files and do
``python run_determine_data.py --help`` to see which options there are. Most are discussed below.

Running step by step
--------------------

Activate a container:

.. code-block:: bash

   source /cvmfs/xenon.opensciencegrid.org/releases/nT/development/setup.sh

Determine which data to process

.. code-block:: bash

   reprox-find-data \
       --package cutax \
       --context xenonnt_v6 \
       --target event_info event_pattern_fit cuts_basic \
       --cmt-version global_v6

This takes a while (+/- 30 minutes) and writes a file
called ``/dali/lgrandi/xenonnt/data_management_reprocessing/to_do_runs.csv`` (depending on your ini
file)

.. code-block:: bash

   reprox-start-jobs \
       --package cutax \
       --context xenonnt_v6 \
       --target event_info event_pattern_fit cuts_basic \
       --cmt-version global_v6 \
       --ram 12000 \
       --cpu 2

Now, hopefully most of the data has been processed successfully, we can now move it to the
production folder

.. code-block:: bash

   reporx-move-folders

Or run it as a single command

.. code-block:: bash

   reprox-reprocess \
       --package cutax \
       --context xenonnt_v6 \
       --target event_info event_pattern_fit cuts_basic \
       --cmt-version global_v6 \
       --ram 12000 \
       --cpu 2 \
       --move-after-workflow # To move the data into the production folder

Advanced usage
==============

Below are several more advanced use cases.

Changing the defaults of processing
-----------------------------------

You might want to play with the config file that says how many resources one uses by default.
The `\ ``reprocessing.ini`` <https://github.com/XENONnT/reprox/blob/master/reprox/reprocessing.ini>`_ file. 
You can either change the source code of this file, or you can overwrite it as follows:

.. code-block:: bash

   git clone git@github.com:XENONnT/reprox.git
   cp reprox/reprox/reprocessing.ini my_reprocessing_config.ini

   # # Edit my_reprocessing_config.ini. For example using vim:
   # vi my_reprocessing_config.ini 

   # overwrite the file used using an environment variable
   export export REPROX_CONFIG=$(pwd)/my_reprocessing_config.ini

You will see that your defaults have been changed (e.g. do ``reprox-reprocess --help``\ ) reflecting the changes you made in the ``.ini`` file.

Use custom config
-----------------

You might want to process some data with slightly different settings, this can be done using
the\ ``--context_kwargs`` argument as follows
(please don't move it into the production folder unless you know what you are doing):

.. code-block:: bash

   reprox-reprocess \
       --package cutax \
       --context xenonnt_v6 \
       --target event_info event_pattern_fit cuts_basic \
       --cmt-version global_v6 \
       --ram 12000 \
       --cpu 2 
       --context_kwargs '{"s1_min_coincidence": 2, "s2_min_pmts": 10}'

Using ``reprox`` from your jupyter notebook
-----------------------------------------------

You can also run the commands from above in a notebook or python script.

.. code-block:: python

   from reprox import find_data, submit_jobs, validate_run

   targets = 'event_info event_pattern_fit cuts_basic'.split()

   # First determine which data to process
   find_data.main(
       targets=targets,
       exclude_from_invalid_cmt_version='global_v6'
   )
   # Now start running the jobs
   submit_jobs.submit_jobs(targets=targets)

   # Finally move the jobs to the production folder
   validate_run.move_all.main()

Processing NV data
------------------

By default, the package assumes that only linked-mode or TPC runs are processed, if you want to
instead process NV data you need to tell the scripts to also take into account the NV detector:

.. code-block:: bash

   reprox-reprocess \
       --package cutax \
       --context xenonnt_v6 \
       --target events_nv \
       --detectors neutron_veto muon_veto
       --ram 12000 \
       --cpu 2

Using tagged versions
---------------------

One might want to run with a different tag as so

.. code-block:: bash

   source /cvmfs/xenon.opensciencegrid.org/releases/nT/2021.12.2/setup.sh
   reprox-reprocess \
       --package cutax  \
       --context xenonnt_v5  \
       --targets event_info \
       --cmt-version global_v5 \
       --ram 24000  \
       --cpu 2  \
       --move-after-workflow \
       --tag 2021.12.2
