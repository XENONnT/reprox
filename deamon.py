import time
import os

if __name__ == '__main__':
	while True:
		os.system('/home/yuanlq/software/reprox/bin/reprox-reprocess --context xenonnt_offline')
		print('Finished! Sleep for 10 minutes...')
		time.sleep(600)
