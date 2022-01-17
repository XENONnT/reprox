import os

import pandas as pd
from reprox import core, submit_jobs
from bson import json_util
import unittest
import strax
import json


class TestSubmitJobs(unittest.TestCase):
    context = 'xenonnt_online'
    package = 'straxen'
    target = 'event_info_double'
    dummy_md = os.path.join(os.path.abspath('.'), '.dummy_md.json')
    runs = tuple(f'{r:06}' for r in range(20_000, 20_050))

    def setUp(self) -> None:
        self.write_csv()
        self.write_dummy_json(self.dummy_md, {'chunks': [{'n': 0}]})
        st = self.get_context()

        keys = [st.key_for('run_id', t) for t in strax.to_str_tuple(self.target)]

        # Hack new command format
        command = """
cd {base_folder}
echo \
    {run_name} \
    --target {target} \
    --context {context} \
    --package {package} \
    --timeout {timeout} 
    {extra_options}
"""
        dest_folder = os.path.join(core.config['context']['base_folder'], 'strax_data')
        for k in keys:
            data_dir = dest_folder + '/{run_name}' + f'-{k.data_type}-{k.lineage_hash}'
            command += f'\nmkdir {data_dir}'
            command += f'\ncp {self.dummy_md} {data_dir}/{k.data_type}-{k.lineage_hash}-metadata.json'
        command += "\necho Processing job ended"
        core.command = command

    def tearDown(self) -> None:
        if os.path.exists(self.dummy_md):
            os.remove(self.dummy_md)

    def test_submit_jobs(self):
        submit_jobs.submit_jobs()

    def get_context(self):
        return core.get_context(context=self.context, package=self.package)

    def write_csv(self):
        runs = pd.DataFrame({'name': self.runs,
                             'number': [int(r) for r in self.runs]
                             })
        pd.DataFrame(runs).to_csv(core.runs_csv)

    def write_dummy_json(self, path:str, content: dict)->None:
        folder = os.path.split(path)[0]
        os.makedirs(folder, exist_ok=True)

        with open(path, mode='w') as f:
            f.write(json.dumps(content, default=json_util.default))