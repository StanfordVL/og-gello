#!/bin/bash
source ~/miniconda3/bin/activate 
conda activate omnigibson
python reboot_dynamixel.py 

python experiments/run_joylo.py --joint_config_file joint_config_joylo.yaml

