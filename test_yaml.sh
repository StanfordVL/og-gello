#!/bin/bash

# Path to your YAML file
YAML_FILE="sampled_task/available_tasks.yaml"

# Use yq to extract all the top-level keys (scenes)
scene_titles=$(yq e 'keys | .[]' "$YAML_FILE")

# Print the scene titles
echo "Scene titles in the list:"
for title in $scene_titles; do
  echo "$title"
done
