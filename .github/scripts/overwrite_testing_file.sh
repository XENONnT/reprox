test_dir=test_folder
mkdir $test_dir
cp reprox/reprocessing.ini $test_dir/test.ini
current_directory=$(pwd)
sed -i "s#base_folder.*#base_folder=$current_directory/$test_dir#" $test_dir/test.ini
sed -i "s#destination_folder.*#destination_folder=$current_directory/$test_dir#" $test_dir/test.ini