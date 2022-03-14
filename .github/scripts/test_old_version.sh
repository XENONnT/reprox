# install old straxen version and run the tests
if [$1 != 'latest']
then
pip install straxen==$1
python -c "from straxen import print_versions; print_versions()"
coverage run --source=reprox,bin setup.py test -v
coveralls --service=github
fi