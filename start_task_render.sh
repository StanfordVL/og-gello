#!/bin/bash
source ~/miniconda3/bin/activate 
conda activate omnigibson
time_string=$(date +%s%N)
host_name=$(hostname)    # Get the hostname of the machine

SAVE_FOLDER="${HOME}/tmp_data"
mkdir -p ${SAVE_FOLDER}

# Define lists of available tasks and operators
# Path to your YAML file
YAML_FILE="sampled_task/available_tasks.yaml"

# Use yq to extract all the top-level keys (scenes)
TASK_LIST=($(yq e 'keys | .[]' "$YAML_FILE"))
OPERATOR_LIST=("Deyu" "Shine" "Kris" "Yibo" "Mark" "Test")

print_usage() {
  echo "Interactive script to record ROS episodes"
  echo "You will be prompted to:"
  echo "1. Select a task from the available list"
  echo "2. Select an operator from the available list"
}

# Print welcome message and usage
echo "Welcome to the ROS Episode Recording Script"
print_usage

# # Select task
# echo -e "\nAvailable tasks:"
# select TASK_NAME in "${TASK_LIST[@]}"; do
#   if [ -n "$TASK_NAME" ]; then
#     break
#   else
#     echo "Invalid selection. Please try again."
#   fi
# done

# Select operator
echo -e "\nAvailable operators:"
select OPERATOR in "${OPERATOR_LIST[@]}"; do
  if [ -n "$OPERATOR" ]; then
    break
  else
    echo "Invalid selection. Please try again."
  fi
done

# Print selected configuration
echo -e "\nConfiguration:"
echo "Task Name          : $TASK_NAME"
echo "Operator          : $OPERATOR"

# Confirm before proceeding
echo -e "\nProceed with this configuration? (y/n)"
read -r confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "Aborted by user."
  exit 0
fi

echo $TASK_NAME
echo $OPERATOR
batch_id=1
echo "{\"operator\": \"${OPERATOR}\", \"batch_id\": \"${batch_id}\", \"timestamp\": \"$time_string\", \"task_name\": \"${TASK_NAME}\", \"host_name\": \"$host_name\"}" > ${SAVE_FOLDER}/batch_${batch_id}__${time_string}__episode.json
# Run the Python script multiple times
echo "Would run: python experiments/launch_nodes.py --batch_id ${batch_id} --recording_path ${SAVE_FOLDER}/batch_${batch_id}__${time_string}.hdf5"
python experiments/launch_nodes.py --batch_id ${batch_id} --recording_path ${SAVE_FOLDER}/batch_${batch_id}__${time_string}.hdf5


echo "All iterations completed successfully."
