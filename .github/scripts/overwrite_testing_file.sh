test_dir=$1
test_file=$2
mkdir $test_dir
cp reprox/reprocessing.ini $test_dir/$test_file
current_directory=$(pwd)
sed -i "s#base_folder.*#base_folder=$current_directory/$test_dir#" $test_dir/$test_file
sed -i "s#destination_folder.*#destination_folder=$current_directory/$test_dir#" $test_dir/$test_file
