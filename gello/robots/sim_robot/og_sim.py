import time
import torch as th
import numpy as np
from typing import Dict, Optional
import json

import omnigibson as og
from omnigibson.macros import gm
import omnigibson.lazy as lazy
from omnigibson.envs import DataCollectionWrapper
from omnigibson.robots import REGISTERED_ROBOTS
from omnigibson.robots.r1 import R1
from omnigibson.robots.r1pro import R1Pro
from omnigibson.robots.manipulation_robot import ManipulationRobot
from omnigibson.tasks import BehaviorTask
from omnigibson.systems.system_base import BaseSystem
from omnigibson.utils.teleop_utils import OVXRSystem
from omnigibson.utils.ui_utils import choose_from_options
from omnigibson.object_states import Filled
from omnigibson.prims.xform_prim import XFormPrim
from omnigibson.utils.usd_utils import GripperRigidContactAPI, ControllableObjectViewAPI
import omnigibson.utils.transform_utils as T
from omnigibson.utils.config_utils import parse_config
from omnigibson.utils.python_utils import recursively_convert_to_torch

from gello.robots.sim_robot.zmq_server import ZMQRobotServer, ZMQServerThread

from gello.robots.sim_robot.og_teleop_cfg import *
import gello.robots.sim_robot.og_teleop_utils as utils

from bddl.activity import Conditions


class OGRobotServer:
    def __init__(
        self,
        robot: str = ROBOT_TYPE,
        config: str = None,
        host: str = "127.0.0.1",
        port: int = 5556,
        recording_path: Optional[str] = None,
        task_name: Optional[str] = None,
        partial_load: bool = True,
        batch_id: Optional[int] = None, # 0 or 1
        instance_id: Optional[int] = None,
        ghosting: bool = True,
    ):
        # Case 1: Direct task name provided
        if task_name is not None:
            assert batch_id is None, "Cannot specify both task name and batch id"
            self.task_name = task_name
        # Case 2: Batch ID provided
        elif batch_id is not None:
            assert batch_id in [0, 1], f"Got invalid batch id: {batch_id}. Must be 0 or 1"
            self.task_name = choose_from_options(options=VALIDATED_TASKS[batch_id], 
                                                name="task options", 
                                                random_selection=False)
        # Case 3: No task specified
        else:
            self.task_name = None
        
        # Configure task if one was set
        if self.task_name is not None:
            available_tasks = utils.load_available_tasks()
            assert self.task_name in available_tasks, f"Task {self.task_name} not found in available tasks"
            self.task_cfg = available_tasks[self.task_name][0] # TODO: once we have multiple instances, we need to specify this
        else:
            self.task_cfg = None

        utils.apply_omnigibson_macros()
        
        # Disable a subset of transition rules for data collection
        for rule in DISABLED_TRANSITION_RULES:
            rule.ENABLED = False

        robot_cls = REGISTERED_ROBOTS.get(robot, None)
        assert robot_cls is not None, f"Got invalid OmniGibson robot class: {robot}"
        assert issubclass(robot_cls, ManipulationRobot), f"Robot class {robot} is not a manipulation robot! Cannot use GELLO"
        assert robot in SUPPORTED_ROBOTS, f"Robot {robot} is not supported by GELLO! Supported robots: {SUPPORTED_ROBOTS}"

        if config is None:
            cfg = utils.generate_basic_environment_config(self.task_name, self.task_cfg)
        else:
            # Load config from file
            cfg = parse_config(config)

        robot_config = utils.generate_robot_config(self.task_name, self.task_cfg)
        cfg["robots"] = [robot_config]

        if partial_load:
            cfg["scene"]["load_room_types"] = utils.get_task_relevant_room_types(activity_name=self.task_name)

        self.env = og.Environment(configs=cfg)
        self.robot = self.env.robots[0]
        self.instance_id = instance_id - 1      # This will be immediately incremented during our first reset() during initialization

        self.ghosting = ghosting
        if self.ghosting:
            self.ghost = utils.setup_ghost_robot(self.env.scene, self.task_cfg)
            og.sim.step() # Initialize ghost robot
            self._ghost_appear_counter = {arm: 0 for arm in self.robot.arm_names}
            self.ghost_info = utils.setup_ghost_robot_info(self.ghost, self.robot)

        # Handle fluid object if needed
        if USE_FLUID:
            obj = self.env.scene.object_registry("name", "obj")
            water = self.env.scene.get_system("water")
            obj.states[Filled].set_value(water, True)
            for _ in range(50):
                og.sim.step()
            self.env.scene.update_initial_state()

        # Set up cameras, visualizations, and UI
        self._setup_teleop_support()
        
        # Set up status display
        self.status_window, self.status_labels = utils.setup_status_display_ui(og.sim.viewer_camera._viewport)
        self.event_queue = []

        # Set variables that are set during reset call
        self._reset_max_arm_delta = DEFAULT_RESET_DELTA_SPEED * (np.pi / 180) * og.sim.get_sim_step_dt()
        self._resume_cooldown_time = None
        self._in_cooldown = False
        self._current_trunk_translate = DEFAULT_TRUNK_TRANSLATE
        self._current_trunk_tilt_offset = 0.0
        self._current_trunk_tilt = 0.0
        self._joint_state = None
        self._joint_cmd = None
        self._waiting_to_resume = True
        self._should_update_checkpoint = False

        # Recording configuration
        self._recording_path = recording_path
        if self._recording_path is not None:
            self.env = DataCollectionWrapper(
                env=self.env, 
                output_path=self._recording_path, 
                viewport_camera_path=og.sim.viewer_camera.active_camera_path,
                only_successes=False,
                flush_every_n_traj=1,
                use_vr=VIEWING_MODE == ViewingMode.VR,
                keep_checkpoint_rollback_data=True,
            )

        # Status tracking
        self._prev_grasp_status = {arm: False for arm in self.robot.arm_names}
        self._prev_in_hand_status = {arm: False for arm in self.robot.arm_names}
        self._frame_counter = 0
        self._prev_base_motion = False
        self._cam_switched = False
        self._button_toggled_state = {
            "x": False,
            "y": False,
            "a": False,
            "b": False,
            "left": False,
            "right": False,
        }

        # Set default active arm
        self.active_arm = "right"
        self._arm_shoulder_directions = {"left": -1.0, "right": 1.0}
        self.obs = {}
        
        # Cache values
        qpos_min, qpos_max = self.robot.joint_lower_limits, self.robot.joint_upper_limits
        self._trunk_tilt_limits = {"lower": qpos_min[self.robot.trunk_control_idx][2],
                                   "upper": qpos_max[self.robot.trunk_control_idx][2]}
        self._arm_joint_limits = dict()
        for arm in self.robot.arm_names:
            self._arm_joint_limits[arm] = {
                "lower": qpos_min[self.robot.arm_control_idx[arm]],
                "upper": qpos_max[self.robot.arm_control_idx[arm]],
            }

        with og.sim.stopped():
            # # Set lower position iteration count for faster sim speed
            # og.sim._physics_context._physx_scene_api.GetMaxPositionIterationCountAttr().Set(8)
            # og.sim._physics_context._physx_scene_api.GetMaxVelocityIterationCountAttr().Set(1)
            isregistry = lazy.carb.settings.acquire_settings_interface()
            isregistry.set_int(lazy.omni.physx.bindings._physx.SETTING_NUM_THREADS, 0)
            # isregistry.set_int(lazy.omni.physx.bindings._physx.SETTING_MIN_FRAME_RATE, int(1 / og.sim.get_physics_dt()))
            # isregistry.set_int(lazy.omni.physx.bindings._physx.SETTING_MIN_FRAME_RATE, 30)

            # Enable CCD for all task-relevant objects
            if isinstance(self.env.task, BehaviorTask):
                for bddl_obj in self.env.task.object_scope.values():
                    if not bddl_obj.is_system and bddl_obj.exists:
                        for link in bddl_obj.wrapped_obj.links.values():
                            link.ccd_enabled = True
            # Postprocessing robot and objects
            for obj in self.env.scene.objects:
                if obj != self.robot:
                    if obj.category in VISUAL_ONLY_CATEGORIES:
                        obj.visual_only = True
                else:
                    if isinstance(obj, (R1, R1Pro)):
                        obj.base_footprint_link.mass = 250.0

            # Update ghost robot's masses to be uniform to avoid orthonormal errors
            if self.ghosting:
                for link in self.ghost.links.values():
                    link.mass = 0.1

        # Make sure robot fingers are extra grippy
        gripper_mat = lazy.isaacsim.core.api.materials.PhysicsMaterial(
            prim_path=f"{self.robot.prim_path}/Looks/gripper_mat",
            name="gripper_material",
            static_friction=2.0,
            dynamic_friction=1.0,
            restitution=None,
        )
        for _, links in self.robot.finger_links.items():
            for link in links:
                for msh in link.collision_meshes.values():
                    msh.apply_physics_material(gripper_mat)

        # Set optimized settings
        utils.optimize_sim_settings(vr_mode=(VIEWING_MODE == ViewingMode.VR))

        # Reset environment to initialize
        self.reset()

        # Take a single step
        action = self.get_action()
        self.env.step(action)

        # Set up keyboard handlers
        self._setup_keyboard_handlers()

        # Set up VR system if needed
        self._setup_vr()
        
        # For some reason, toggle buttons get warped in terms of their placement -- we have them snap to their original
        # locations by setting their scale
        from omnigibson.object_states import ToggledOn
        for obj in self.env.scene.objects:
            if ToggledOn in obj.states:
                scale = obj.states[ToggledOn].visual_marker.scale
                obj.states[ToggledOn].visual_marker.scale = scale

        # Create ZMQ server for communication
        self._zmq_server = ZMQRobotServer(robot=self, host=host, port=port, verbose=False)
        self._zmq_server_thread = ZMQServerThread(self._zmq_server)

    def _setup_teleop_support(self):
        """Set up cameras, visualizations, UI elements"""
        # Setup cameras
        self.camera_paths, self.viewports = utils.setup_cameras(
            self.robot, 
            self.env.external_sensors, 
            RESOLUTION
        )
        self.active_camera_id = 0

        # Setup visualizers
        self.vis_elements = utils.setup_robot_visualizers(self.robot, self.env.scene)
        self.eef_cylinder_geoms = self.vis_elements["eef_cylinder_geoms"]
        self.vis_mats = self.vis_elements["vis_mats"]
        self.vertical_visualizers = self.vis_elements["vertical_visualizers"]
        self.reachability_visualizers = self.vis_elements["reachability_visualizers"]

        # Setup flashlights
        self.flashlights = utils.setup_flashlights(self.robot)
        
        # Setup task-related elements if task is specified
        if self.task_name is not None:
            # Setup task instruction UI
            self.overlay_window, self.text_labels, self.bddl_goal_conditions = utils.setup_task_instruction_ui(
                self.task_name, 
                self.env, 
                self.robot
            )
            
            # Initialize goal status tracking
            self._prev_goal_status = {
                'satisfied': [],
                'unsatisfied': list(range(len(self.bddl_goal_conditions)))
            }
            
            # Get task-relevant objects
            task_objects = [bddl_obj.wrapped_obj for bddl_obj in self.env.task.object_scope.values() 
                            if bddl_obj.wrapped_obj is not None]
            
            self.task_relevant_objects = [obj for obj in task_objects 
                                          if not isinstance(obj, BaseSystem)
                                          and obj.category != "agent" 
                                          and obj.category not in EXTRA_TASK_RELEVANT_CATEGORIES]
            
            # Setup object beacons
            self.object_beacons = utils.setup_object_beacons(self.task_relevant_objects, self.env.scene)
            
            # Setup task-specific visualizers
            self.task_visualizers = utils.setup_task_visualizers(self.task_relevant_objects, self.env.scene)
            
            # Get task-irrelevant objects
            self.task_irrelevant_objects = [obj for obj in self.env.scene.objects
                                            if not isinstance(obj, BaseSystem)
                                            and obj not in task_objects
                                            and obj.category not in EXTRA_TASK_RELEVANT_CATEGORIES]
        else:
            self.overlay_window = None
            self.text_labels = None
            self.bddl_goal_conditions = None
            self.task_relevant_objects = []
            self.task_irrelevant_objects = []
            self.object_beacons = {}

    def _setup_keyboard_handlers(self):
        """Set up keyboard event handlers"""
        def keyboard_event_handler(event, *args, **kwargs):
            # Check if we've received a key press or repeat
            if (
                    event.type == lazy.carb.input.KeyboardEventType.KEY_PRESS
                    or event.type == lazy.carb.input.KeyboardEventType.KEY_REPEAT
            ):
                if event.input == lazy.carb.input.KeyboardInput.R:
                    self.reset()
                elif event.input == lazy.carb.input.KeyboardInput.X:
                    self.resume_control()
                elif event.input == lazy.carb.input.KeyboardInput.ESCAPE:
                    self.stop()

            # Callback always needs to return True
            return True

        appwindow = lazy.omni.appwindow.get_default_app_window()
        input_interface = lazy.carb.input.acquire_input_interface()
        keyboard = appwindow.get_keyboard()
        self.sub_keyboard = input_interface.subscribe_to_keyboard_events(keyboard, keyboard_event_handler)

    def _setup_vr(self):
        """Set up VR system if needed"""
        self.vr_system = None
        self.camera_prims = []
        
        if VIEWING_MODE == ViewingMode.VR:
            for cam_path in self.camera_paths:
                cam_prim = XFormPrim(
                    relative_prim_path=utils.absolute_prim_path_to_scene_relative(
                        self.robot.scene, cam_path
                    ),
                    name=cam_path,
                )
                cam_prim.load(self.robot.scene)
                self.camera_prims.append(cam_prim)
            
            self.vr_system = OVXRSystem(
                robot=self.robot,
                show_control_marker=False,
                system="SteamVR",
                eef_tracking_mode="disabled",
                align_anchor_to=self.camera_prims[0],
            )
            self.vr_system.start()

    def num_dofs(self) -> int:
        """Return the number of degrees of freedom"""
        return self.robot.n_joints

    def get_joint_state(self) -> th.tensor:
        """Get the current joint state"""
        return self._joint_state

    def command_joint_state(self, joint_state: th.tensor, component=None) -> None:
        """
        Command the robot to a joint state
        
        Args:
            joint_state: Target joint state
            component: Which component to control (optional)
        """
        # If R1, process manually
        state = joint_state.clone()
        if isinstance(self.robot, R1) and not isinstance(self.robot, R1Pro):
            # [ 6DOF left arm, 6DOF right arm, 3DOF base, 2DOF trunk (z, ry), 2DOF gripper, X, Y, B, A, home, left arrow, right arrow buttons]
            start_idx = 0
            for component, dim in zip(
                    ("left_arm", "right_arm", "base", "trunk", "left_gripper", "right_gripper", "button_x", "button_y", "button_b", "button_a", "button_capture", "button_home", "button_left", "button_right"),
                    (6, 6, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
            ):
                if start_idx >= len(state):
                    break
                self._joint_cmd[component] = state[start_idx: start_idx + dim]
                start_idx += dim
        elif isinstance(self.robot, R1Pro):
            # [ 7DOF left arm, 7DOF right arm, 3DOF base, 2DOF trunk (z, ry), 2DOF gripper, X, Y, B, A, home, left arrow, right arrow buttons]
            start_idx = 0
            for component, dim in zip(
                    ("left_arm", "right_arm", "base", "trunk", "left_gripper", "right_gripper", "button_x", "button_y", "button_b", "button_a", "button_capture", "button_home", "button_left", "button_right"),
                    (7, 7, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
            ):
                if start_idx >= len(state):
                    break
                self._joint_cmd[component] = state[start_idx: start_idx + dim]
                start_idx += dim
        else:
            # Sort by component
            if component is None:
                component = self.active_arm
            assert component in self._joint_cmd, \
                f"Got invalid component joint cmd: {component}. Valid options: {self._joint_cmd.keys()}"
            self._joint_cmd[component] = joint_state.clone()

    def freedrive_enabled(self) -> bool:
        """Check if freedrive mode is enabled"""
        return True

    def set_freedrive_mode(self, enable: bool):
        """Set freedrive mode"""
        pass

    def get_observations(self) -> Dict[str, th.tensor]:
        """Get the current observations"""
        return self.obs

    def _update_observations(self) -> Dict[str, th.tensor]:
        """Update observations with current robot state"""
        # Loop over all arms and grab relevant joint info
        joint_pos = self.robot.get_joint_positions()
        joint_vel = self.robot.get_joint_velocities()
        finger_impulses = GripperRigidContactAPI.get_all_impulses(self.env.scene.idx) if INCLUDE_CONTACT_OBS else None

        obs = dict()
        obs["active_arm"] = self.active_arm
        obs["in_cooldown"] = self._in_cooldown
        obs["base_contact"] = any(len(link.contact_list()) > 0 for link in self.robot.non_floor_touching_base_links) if INCLUDE_CONTACT_OBS else False
        obs["trunk_contact"] = any(len(link.contact_list()) > 0 for link in self.robot.trunk_links) if INCLUDE_CONTACT_OBS else False
        obs["reset_joints"] = bool(self._joint_cmd["button_y"][0].item())
        obs["waiting_to_resume"] = self._waiting_to_resume

        for i, arm in enumerate(self.robot.arm_names):
            arm_control_idx = self.robot.arm_control_idx[arm]
            obs[f"arm_{arm}_control_idx"] = arm_control_idx
            obs[f"arm_{arm}_joint_positions"] = joint_pos[arm_control_idx]
            # Account for tilt offset
            obs[f"arm_{arm}_joint_positions"][0] -= self._current_trunk_tilt * self._arm_shoulder_directions[arm]
            obs[f"arm_{arm}_joint_velocities"] = joint_vel[arm_control_idx]
            obs[f"arm_{arm}_gripper_positions"] = joint_pos[self.robot.gripper_control_idx[arm]]
            obs[f"arm_{arm}_ee_pos_quat"] = th.concatenate(self.robot.eef_links[arm].get_position_orientation())
            # When using VR, this expansive check makes the view glitch
            obs[f"arm_{arm}_contact"] = any(len(link.contact_list()) > 0 for link in self.robot.arm_links[arm]) if VIEWING_MODE != ViewingMode.VR and INCLUDE_CONTACT_OBS else False
            obs[f"arm_{arm}_finger_max_contact"] = th.max(th.sum(th.square(finger_impulses[:, 2*i:2*(i+1), :]), dim=-1)).item() if INCLUDE_CONTACT_OBS else 0.0

            obs[f"{arm}_gripper"] = self._joint_cmd[f"{arm}_gripper"].item()

        if INCLUDE_JACOBIAN_OBS:
            for arm in self.robot.arm_names:
                link_name = self.robot.eef_link_names[arm]

                start_idx = 0 if self.robot.fixed_base else 6
                link_idx = self.robot._articulation_view.get_body_index(link_name)
                jacobian = ControllableObjectViewAPI.get_relative_jacobian(
                    self.robot.articulation_root_path
                )[-(self.robot.n_links - link_idx), :, start_idx : start_idx + self.robot.n_joints]
                
                jacobian = jacobian[:, self.robot.arm_control_idx[arm]]
                obs[f"arm_{arm}_jacobian"] = jacobian

        self.obs = obs

    def resume_control(self):
        """Resume control after waiting"""
        if self._waiting_to_resume:
            self._waiting_to_resume = False
            self._resume_cooldown_time = time.time() + N_COOLDOWN_SECS
            self._in_cooldown = True
            utils.add_status_event(self.event_queue, "waiting", "Control Resumed, cooling down...")

    def serve(self) -> None:
        """Main serving loop"""
        # Start the zmq server
        self._zmq_server_thread.start()
        
        while True:
            self._update_observations()

            # Process button inputs
            self._process_button_inputs()
            
            # Update status display
            self.event_queue = utils.update_status_display(
                self.status_window,
                self.status_labels,
                self.event_queue,
                time.time()
            )
            
            # Only decrement cooldown if we're not waiting to resume
            if not self._waiting_to_resume:
                if self._in_cooldown:
                    utils.print_color(f"\rIn cooldown!{' ' * 40}", end="", flush=True)
                    self._in_cooldown = time.time() < self._resume_cooldown_time
                else:
                    utils.print_color(f"\rRunning!{' ' * 40}", end="", flush=True)

            # If waiting to resume, simply step sim without updating action
            if self._waiting_to_resume:
                og.sim.render()
                utils.print_color(f"\rPress X (keyboard or JoyCon) to resume sim!{' ' * 30}", end="", flush=True)
                utils.add_status_event(self.event_queue, "waiting", "Waiting to Resume... Press X to start", persistent=True)
            else:
                # Generate action and deploy
                action = self.get_action()
                _, _, _, _, info = self.env.step(action)
                
                # Update checkpoint if queued
                if self._should_update_checkpoint:
                    self.env.update_checkpoint()
                    print("Auto recorded checkpoint due to goal status change!")
                    if self.event_queue:
                        utils.add_status_event(self.event_queue, "checkpoint", "Checkpoint Recorded due to goal status change")
                    self._should_update_checkpoint = False

                # Update visualizations and status
                self._update_visualization_and_status(info)

    def _process_button_inputs(self):
        """Process button inputs from controller"""
        # If X is toggled from OFF -> ON, either:
        # (a) begin receiving commands, if currently paused, or
        # (b) record checkpoint, if actively running
        button_x_state = self._joint_cmd["button_x"].item() != 0.0
        if button_x_state and not self._button_toggled_state["x"]:
            if self._waiting_to_resume:
                self.resume_control()
            else:
                if self._recording_path is not None:
                    self.env.update_checkpoint()
                    print("Checkpoint Recorded manually")
                    utils.add_status_event(self.event_queue, "checkpoint", "Checkpoint Recorded manually")
        self._button_toggled_state["x"] = button_x_state

        # If Y is toggled from OFF -> ON, rollback to checkpoint
        button_y_state = self._joint_cmd["button_y"].item() != 0.0
        if button_y_state and not self._button_toggled_state["y"]:
            if self._recording_path is not None:
                print("Rolling back to latest checkpoint...watch out, GELLO will move on its own!")
                utils.add_status_event(self.event_queue, "rollback", "Rolling back to latest checkpoint...watch out, GELLO will move on its own!")
                self.env.rollback_to_checkpoint()
                utils.optimize_sim_settings(vr_mode=(VIEWING_MODE == ViewingMode.VR))
                
                # Extract trunk position values and calculate offsets
                trunk_qpos = self.robot.get_joint_positions()[self.robot.trunk_control_idx]
                self._current_trunk_translate = utils.infer_trunk_translate_from_torso_qpos(trunk_qpos)
                base_trunk_pos = utils.infer_torso_qpos_from_trunk_translate(self._current_trunk_translate)
                self._current_trunk_tilt_offset = float(trunk_qpos[2] - base_trunk_pos[2])
                
                print("Finished rolling back!")
                self._waiting_to_resume = True
        self._button_toggled_state["y"] = button_y_state

        # If B is toggled from OFF -> ON, toggle camera
        button_b_state = self._joint_cmd["button_b"].item() != 0.0
        if button_b_state and not self._button_toggled_state["b"]:
            self.active_camera_id = 1 - self.active_camera_id
            og.sim.viewer_camera.active_camera_path = self.camera_paths[self.active_camera_id]
            if VIEWING_MODE == ViewingMode.VR:
                self.vr_system.set_anchor_with_prim(
                    self.camera_prims[self.active_camera_id]
                )
        self._button_toggled_state["b"] = button_b_state

        # If A is toggled from OFF -> ON, toggle task-irrelevant object visibility
        button_a_state = self._joint_cmd["button_a"].item() != 0.0
        if button_a_state and not self._button_toggled_state["a"]:
            for obj in self.task_irrelevant_objects:
                obj.visible = not obj.visible
            for obj in self.task_relevant_objects:
                obj.highlighted = not obj.highlighted
                beacon = self.object_beacons[obj]
                beacon.set_position_orientation(
                    position=obj.aabb_center + th.tensor([0, 0, BEACON_LENGTH / 2.0]),
                    orientation=T.euler2quat(th.tensor([0, 0, 0])),
                    frame="world"
                )
                beacon.visible = not beacon.visible
                if obj.fixed_base and obj.articulated:
                    for name, link in obj.links.items():
                        if not 'meta' in name and link != obj.root_link:
                            link.visible = not obj.highlighted
        self._button_toggled_state["a"] = button_a_state

        # If capture is toggled from OFF -> ON, breakpoint
        if self._joint_cmd["button_capture"].item() != 0.0:
            if not self._in_cooldown:
                breakpoint()

        # If home is toggled from OFF -> ON, reset env
        if self._joint_cmd["button_home"].item() != 0.0:
            if not self._in_cooldown:
                self.reset()

        # If left arrow is toggled from OFF -> ON, toggle flashlight on left eef
        button_left_arrow_state = self._joint_cmd["button_left"].item() != 0.0
        if button_left_arrow_state and not self._button_toggled_state["left"]:
            if self.flashlights["left"].GetVisibilityAttr().Get() == "invisible":
                self.flashlights["left"].MakeVisible()
            else:
                self.flashlights["left"].MakeInvisible()
        self._button_toggled_state["left"] = button_left_arrow_state
        
        # If right arrow is toggled from OFF -> ON, toggle flashlight on right eef
        button_right_arrow_state = self._joint_cmd["button_right"].item() != 0.0
        if button_right_arrow_state and not self._button_toggled_state["right"]:
            if self.flashlights["right"].GetVisibilityAttr().Get() == "invisible":
                self.flashlights["right"].MakeVisible()
            else:
                self.flashlights["right"].MakeInvisible()
        self._button_toggled_state["right"] = button_right_arrow_state

    def _update_visualization_and_status(self, info):
        """Update visualization and status based on new information"""
        # Update task goal status if task is active
        if self.task_name is not None and 'done' in info:
            current_goal_status = utils.update_goal_status(
                self.text_labels,
                info['done']['goal_status'],
                self._prev_goal_status,
                self.env,
                self._recording_path,
                self.event_queue
            )
            
            # Update checkpoint if new goals are satisfied
            if AUTO_CHECKPOINTING and len(current_goal_status['satisfied']) > len(self._prev_goal_status['satisfied']):
                if self._recording_path is not None:
                    self._should_update_checkpoint = True
            
            self._prev_goal_status = current_goal_status
        
        # Update other visualization elements
        self._prev_in_hand_status = utils.update_in_hand_status(
            self.robot,
            self.vis_mats,
            self._prev_in_hand_status
        )
        
        self._prev_grasp_status = utils.update_grasp_status(
            self.robot,
            self.eef_cylinder_geoms,
            self._prev_grasp_status
        )
        
        self._prev_base_motion = utils.update_reachability_visualizers(
            self.reachability_visualizers,
            self._joint_cmd,
            self._prev_base_motion
        )
        
        # Update checkpoint if needed
        self._frame_counter = utils.update_checkpoint(
            self.env,
            self._frame_counter,
            self._recording_path,
            self.event_queue
        )

    def get_action(self):
        """
        Generate action based on current joint commands
        
        Returns:
            torch.Tensor: Action for the robot
        """
        # Start an empty action
        action = th.zeros(self.robot.action_dim)

        # Apply arm action + extra dimension from base
        if isinstance(self.robot, R1):
            # Apply arm action
            left_act = self._joint_cmd["left_arm"].clone().clip(self._arm_joint_limits["left"]["lower"], self._arm_joint_limits["left"]["upper"])
            right_act = self._joint_cmd["right_arm"].clone().clip(self._arm_joint_limits["right"]["lower"], self._arm_joint_limits["right"]["upper"])

            # If we're in cooldown, clip values based on max delta value
            if self._in_cooldown:
                robot_pos = self.robot.get_joint_positions()
                robot_left_pos, robot_right_pos = [robot_pos[self.robot.arm_control_idx[arm]] for arm in ("left", "right")]
                robot_left_delta = left_act - robot_left_pos
                robot_right_delta = right_act - robot_right_pos
                left_act = robot_left_pos + robot_left_delta.clip(-self._reset_max_arm_delta, self._reset_max_arm_delta)
                right_act = robot_right_pos + robot_right_delta.clip(-self._reset_max_arm_delta, self._reset_max_arm_delta)

            left_act[0] += self._current_trunk_tilt * self._arm_shoulder_directions["left"]
            right_act[0] += self._current_trunk_tilt * self._arm_shoulder_directions["right"]
            action[self.robot.arm_action_idx["left"]] = left_act
            action[self.robot.arm_action_idx["right"]] = right_act

            # Apply base action
            action[self.robot.base_action_idx] = self._joint_cmd["base"].clone()

            # Apply gripper action
            action[self.robot.gripper_action_idx["left"]] = self._joint_cmd["left_gripper"].clone()
            action[self.robot.gripper_action_idx["right"]] = self._joint_cmd["right_gripper"].clone()

            # Apply trunk action
            if SIMPLIFIED_TRUNK_CONTROL:
                # Update trunk translation (height)
                self._current_trunk_translate = float(th.clamp(
                    th.tensor(self._current_trunk_translate, dtype=th.float) - 
                    th.tensor(self._joint_cmd["trunk"][0].item() * og.sim.get_sim_step_dt(), dtype=th.float),
                    0.0,
                    2.0
                ))
                trunk_action = utils.infer_torso_qpos_from_trunk_translate(self._current_trunk_translate)
                
                # Update trunk tilt offset
                self._current_trunk_tilt_offset = float(th.clamp(
                    th.tensor(self._current_trunk_tilt_offset, dtype=th.float) + 
                    th.tensor(self._joint_cmd["trunk"][1].item() * og.sim.get_sim_step_dt(), dtype=th.float),
                    self._trunk_tilt_limits["lower"] - trunk_action[2],
                    self._trunk_tilt_limits["upper"] - trunk_action[2]
                ))
                trunk_action[2] = trunk_action[2] + self._current_trunk_tilt_offset
                
                action[self.robot.trunk_action_idx] = trunk_action

            # Update vertical visualizers
            if USE_VERTICAL_VISUALIZERS:
                for arm in ["left", "right"]:
                    arm_position = self.robot.eef_links[arm].get_position_orientation(frame="world")[0]
                    self.vertical_visualizers[arm].set_position_orientation(
                        position=arm_position - th.tensor([0, 0, 1.0]), 
                        orientation=th.tensor([0, 0, 0, 1.0]), 
                        frame="world"
                    )
        else:
            action[self.robot.arm_action_idx[self.active_arm]] = self._joint_cmd[self.active_arm].clone()

        # Optionally update ghost robot
        if self.ghosting and self._frame_counter % GHOST_UPDATE_FREQ == 0:
            self._ghost_appear_counter = utils.update_ghost_robot(
                self.ghost, 
                self.robot, 
                action, 
                self._ghost_appear_counter,
                self.ghost_info,
            )

        return action

    def reset(self, increment_instance=True):
        """
        Reset the environment and robot state

        Args:
            increment_instance (bool): If True and self.instance_id is not None, will increment the instance to reset to
                and reset to the updated instance id's initial state
        """
        if self._recording_path is not None:
            reset_text = "Resetting environment, episode recorded"
        else:
            reset_text = "Resetting environment"
        utils.add_status_event(self.event_queue, "reset", reset_text)
        # Reset internal variables
        self._ghost_appear_counter = {arm: 0 for arm in self.robot.arm_names}
        self._resume_cooldown_time = time.time() + N_COOLDOWN_SECS
        self._in_cooldown = True
        self._current_trunk_translate = DEFAULT_TRUNK_TRANSLATE
        self._current_trunk_tilt_offset = 0.0
        self._current_trunk_tilt = 0.0
        self._waiting_to_resume = True
        self._joint_state = self.robot.reset_joint_pos
        self._joint_cmd = {
            f"{arm}_arm": self._joint_state[self.robot.arm_control_idx[arm]] for arm in self.robot.arm_names
        }
        self._should_update_checkpoint = False
        if isinstance(self.robot, (R1, R1Pro)):
            for arm in self.robot.arm_names:
                self._joint_cmd[f"{arm}_gripper"] = th.ones(len(self.robot.gripper_action_idx[arm]))
                self._joint_cmd["base"] = self._joint_state[self.robot.base_control_idx]
                self._joint_cmd["trunk"] = th.zeros(2)
                self._joint_cmd["button_x"] = th.zeros(1)
                self._joint_cmd["button_y"] = th.zeros(1)
                self._joint_cmd["button_b"] = th.zeros(1)
                self._joint_cmd["button_a"] = th.zeros(1)
                self._joint_cmd["button_capture"] = th.zeros(1)
                self._joint_cmd["button_home"] = th.zeros(1)
                self._joint_cmd["button_left"] = th.zeros(1)
                self._joint_cmd["button_right"] = th.zeros(1)

        # Update the instance id / initial state if the instance ID is specified
        # We will manually update the task relevant objects (TRO) state
        if self.instance_id is not None and increment_instance:
            self.instance_id += 1
            scene_model = self.env.task.scene_name
            tro_filename = self.env.task.get_cached_activity_scene_filename(
                scene_model=scene_model,
                activity_name=self.env.task.activity_name,
                activity_definition_id=self.env.task.activity_definition_id,
                activity_instance_id=self.instance_id,
            )
            with open(f"{gm.DATASET_PATH}/scenes/{scene_model}/json/{self.env.task.activity_name}_instances/{tro_filename}-tro_state.json", "r") as f:
                tro_state = recursively_convert_to_torch(json.load(f))
            self.env.scene.reset()
            for bddl_name, obj_state in tro_state.items():
                if "agent" in bddl_name:
                    # Only set pose (we assume this is a holonomic robot, so ignore Rx / Ry and only take Rz component
                    # for orientation
                    robot_pos = obj_state["joint_pos"][:3] + obj_state["root_link"]["pos"]
                    robot_quat = T.euler2quat(th.tensor([0, 0, obj_state["joint_pos"][5]]))
                    self.env.task.object_scope[bddl_name].set_position_orientation(robot_pos, robot_quat)
                else:
                    self.env.task.object_scope[bddl_name].load_state(obj_state, serialized=False)
            self.env.scene.update_initial_file()
            print(f"\nLoading task {self.env.task.activity_name} instance id: {self.instance_id}\n")

        # Reset env
        self.env.reset()

    def stop(self) -> None:
        """Stop the server and clean up resources"""
        self._zmq_server_thread.terminate()
        self._zmq_server_thread.join()
        
        if self._recording_path is not None:
            # Sanity check if we are in the middle of an episode; always flush the current trajectory
            if len(self.env.current_traj_history) > 0:
                self.env.flush_current_traj()

            self.env.save_data()
        
        if VIEWING_MODE == ViewingMode.VR:
            self.vr_system.stop()
        
        og.shutdown()

    def __del__(self) -> None:
        """Clean up when object is deleted"""
        self.stop()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="OmniGibson Robot Server")
    parser.add_argument("--robot", type=str, default=ROBOT_TYPE, help="Robot type")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=5556, help="Server port")
    parser.add_argument("--recording-path", type=str, default=None, help="Path to save recordings")
    parser.add_argument("--task", type=str, default=None, help="Task name")
    parser.add_argument("--no-ghost", action="store_true", help="Disable ghost robot visualization")
    
    args = parser.parse_args()
    
    sim = OGRobotServer(
        robot=args.robot,
        host=args.host,
        port=args.port,
        recording_path=args.recording_path,
        task_name=args.task,
        ghosting=not args.no_ghost
    )
    
    sim.serve()
    print("Server stopped")