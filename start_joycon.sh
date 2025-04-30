#!/bin/bash
source ~/miniconda3/bin/activate 
conda activate omnigibson
python reboot_dynamixel.py 

python experiments/run_r1_gello.py --joint_config_file joint_config_black_gello_newest.yaml
