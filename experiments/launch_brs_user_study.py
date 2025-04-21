from dataclasses import dataclass
from typing import Optional
import tyro
import torch as th
import os
import glob
from datetime import datetime

import omnigibson as og
import omnigibson.lazy as lazy
from omnigibson.utils.teleop_utils import OVXRSystem
from omnigibson.envs import DataCollectionWrapper
from omnigibson.utils.ui_utils import KeyboardEventHandler

BASE_PATH = "/home/yhang/brs_data"

ENV_CONFIG = {
    "scene": {
        "type": "InteractiveTraversableScene",
        "scene_model": "Pomaria_1_int",
        "load_task_relevant_only": False,
    },
    "task": {
        "type": "BehaviorTask",
        "activity_name": "clean_your_house_after_a_wild_party",
        "activity_definition_id": 0,
        "activity_instance_id": 0,
        "predefined_problem": None,
        "online_object_sampling": False,
    },
}

QUEST_ROBOT_CONFIG = [{
    "type": "R1",
    "position": [-3.3066, -1.5145, 0.0422],
    "orientation": [0.0, 0.0, 1.0, 0.0],
    "obs_modalities": ["rgb"],
    "controller_config": {
        "arm_left": {
            "name": "InverseKinematicsController",
            "mode": "absolute_pose",
            "command_input_limits": None,
            "command_output_limits": None,
        },
        "arm_right": {
            "name": "InverseKinematicsController",
            "mode": "absolute_pose",
            "command_input_limits": None,
            "command_output_limits": None,
        },
        "gripper_left": {"name": "MultiFingerGripperController", "command_input_limits": "default"},
        "gripper_right": {"name": "MultiFingerGripperController", "command_input_limits": "default"},
    },
    "action_normalize": False,
    "reset_joint_pos": [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        -1.8000,
        -0.8000,
        0.0000,
        -0.0068,
        0.0059,
        2.6054,
        2.5988,
        -1.4515,
        -1.4478,
        -0.0065,
        0.0052,
        1.5670,
        -1.5635,
        -1.1428,
        1.1610,
        0.0087,
        0.0087,
        0.0087,
        0.0087,
    ],
}]

def setup_recording_path(base_path, user_name):
    """
    Set up the recording path for the user and determine the next trial number.

    Args:
        base_path (str): Base directory for recordings
        user_name (str): Name of the user

    Returns:
        str: Full path for recording file
    """
    # Create user directory if it doesn't exist
    user_dir = os.path.join(base_path, user_name)
    os.makedirs(user_dir, exist_ok=True)

    # Get current date and time
    current_time = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Find existing trial files to determine next trial number
    existing_files = glob.glob(os.path.join(user_dir, f"vision_pro_{user_name}_trial*"))
    trial_numbers = [0]  # Start with 0 in case no files exist

    for file in existing_files:
        try:
            # Extract trial number from filename
            trial_str = file.split("trial")[-1].split("_")[0]
            trial_numbers.append(int(trial_str))
        except (ValueError, IndexError):
            continue

    next_trial = max(trial_numbers) + 1

    # Create filename
    filename = f"vision_pro_{user_name}_trial{next_trial}_{current_time}.hdf5"
    full_path = os.path.join(user_dir, filename)

    return full_path


@dataclass
class Args:
    method: str = "gello" # gello or quest
    user_name: str = "test_user"


def launch_robot_server(args: Args):
    
    recording_path = setup_recording_path(BASE_PATH, args.user_name)
    
    if args.method == "gello":
        from gello.robots.sim_robot.og_sim import OGRobotServer
        server = OGRobotServer(config=ENV_CONFIG, robot="R1", port=6001, host="127.0.0.1", recording_path=recording_path, ghosting=False, brs=True)
        server.serve()
    elif args.method == "quest":
        # TODO: add robot config here
        env = og.Environment(configs=ENV_CONFIG)
        env = DataCollectionWrapper(
                env=env, 
                output_path=recording_path, 
                viewport_camera_path=og.sim.viewer_camera.active_camera_path,
                only_successes=False, 
                use_vr=True,
            )
        
        KeyboardEventHandler.initialize()
        KeyboardEventHandler.add_keyboard_callback(
            key=lazy.carb.input.KeyboardInput.ESCAPE,
            callback_fn=lambda: env.save_data(),
        )
        
        # Hacks for cached scene
        dishwasher = env.scene.object_registry("name", "dishwasher_dngvvi_0")
        dishwasher.visual_only = True
        dishwasher.links["link_0"].visual_only = False
        dishwasher.joints["j_link_0"].friction = 0.1
        teacup_131 = env.scene.object_registry("name", "teacup_131")
        teacup_131.set_position(th.tensor([-12.5, 2.3, 0.54]))
        coffee_table = env.scene.object_registry("name", "coffee_table_gcollb_0")
        coffee_table.links["base_link"].mass = 200.0
        shelf = env.scene.object_registry("name", "shelf_owvfik_1")
        shelf.links["base_link"].mass = 200.0
        og.sim.step()
        
        vrsys = OVXRSystem(
            robot=env.robots[0],
            show_control_marker=True,
            system="SteamVR",
            eef_tracking_mode="controller",
            align_anchor_to="camera",
        )
        vrsys.start()
        while True:
            vrsys.update()
            env.step(vrsys.get_robot_teleop_action())
    else:
        raise NotImplementedError(f"Method {args.method} not implemented.")

def main(args):
    launch_robot_server(args)


if __name__ == "__main__":
    main(tyro.cli(Args))
