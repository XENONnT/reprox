"""
Run prior to building the documentation
"""
import subprocess
import os

pwd = os.path.abspath('.')
print(pwd)
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
print(result)
os.environ['REPROX_CONFIG'] = os.path.join(pwd, '../../', folder, file)
