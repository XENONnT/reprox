"""
Run prior to building the documentation. Very ugly hack to get a testing ini file.
"""
import subprocess
import os

pwd = os.path.abspath('.')
folder = 'test_folder'
file = 'doc_test.ini'
script = f"""
pwd=$(pwd)
cd ../../
bash .github/scripts/overwrite_testing_file.sh {folder} {file}
cd $pwd
"""
result = subprocess.run(
    script,
    shell=True,
    capture_output=True)
print(result.stdout)
print(result.stderr)
os.environ['REPROX_CONFIG'] = os.path.join(pwd, '../../', folder, file)
