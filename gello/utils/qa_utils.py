import omnigibson as og
from omnigibson.envs import EnvMetric
from omnigibson.tasks import BehaviorTask
from omnigibson.utils.usd_utils import RigidContactAPI
from omnigibson.utils.constants import STRUCTURE_CATEGORIES
from omnigibson.utils.backend_utils import _compute_backend as cb
from gello.robots.sim_robot.og_teleop_utils import GHOST_APPEAR_THRESHOLD
import torch as th
import operator


class MotionMetric(EnvMetric):
    def __init__(self, step_dt):
        """
        Args:
            step_dt (float): Amount of time between steps, used to differentiate from pos -> vel -> acc -> jerk
        """
        self.step_dt = step_dt

        super().__init__()

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        step_metrics = dict()
        for i, robot in enumerate(env.robots):
            # Record velocity (we'll derive accel -> jerk at the end of the episode)
            step_metrics[f"robot{i}::pos"] = robot.get_joint_positions()
        return step_metrics

    def _compute_episode_metrics(self, env, episode_info):
        # Derive acceleration -> jerk based on the recorded velocities
        episode_metrics = dict()
        for pos_key, positions in episode_info.items():
            positions = th.stack(positions, dim=0)
            vels = (positions[1:] - positions[:-1]) / self.step_dt
            accs = (vels[1:] - vels[:-1]) / self.step_dt
            jerks = (accs[1:] - accs[:-1]) / self.step_dt
            # Only keep absolute values
            vels, accs, jerks = th.abs(vels), th.abs(accs), th.abs(jerks)
            episode_metrics[f"{pos_key}::vel_avg"] = vels.mean(dim=0)
            episode_metrics[f"{pos_key}::acc_avg"] = accs.mean(dim=0)
            episode_metrics[f"{pos_key}::jerk_avg"] = jerks.mean(dim=0)
            episode_metrics[f"{pos_key}::vel_std"] = vels.std(dim=0)
            episode_metrics[f"{pos_key}::acc_std"] = accs.std(dim=0)
            episode_metrics[f"{pos_key}::jerk_std"] = jerks.std(dim=0)
            episode_metrics[f"{pos_key}::vel_max"] = vels.max(dim=0)
            episode_metrics[f"{pos_key}::acc_max"] = accs.max(dim=0)
            episode_metrics[f"{pos_key}::jerk_max"] = jerks.max(dim=0)

        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            vel_max_limit=None,
            acc_max_limit=None,
            jerk_max_limit=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            vel_max_limit (None or float): If specified, maximum velocity that is acceptable for the episode to
                be validated
            acc_max_limit (None or float): If specified, maximum acceleration that is acceptable for the episode to
                be validated
            jerk_max_limit (None or float): If specified, maximum jerk that is acceptable for the episode to
                be validated

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        for val_max_limit, val_name in zip((vel_max_limit, acc_max_limit, jerk_max_limit), ("vel_max", "acc_max", "jerk_max")):
            if val_max_limit is not None:
                for name, metric in episode_metrics.items():
                    if f"::{val_name}" in name:
                        test_name = name
                        success = th.all(metric <= val_max_limit).item()
                        feedback = None if success else f"Robot's {val_name} is too high ({metric}), must be <= {val_max_limit}"
                        results[test_name] = {"success": success, "feedback": feedback}

        return results


class CollisionMetric(EnvMetric):
    def __init__(self):
        self.checks = dict()
        super().__init__()

    def add_check(self, name, check):
        """
        Adds a collision check to this metric, which can be queried by @name

        Args:
            name (str): name of the check
            check (function): Collision checker function, with the following signature:

                def check(env: Environment) -> bool

                which should return True if there is collision, else False
        """
        self.checks[name] = check

    def remove_check(self, name):
        """
        Removes check with corresponding @name

        Args:
            name (str): name of the check to remove
        """
        self.checks.pop(name)

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        step_metrics = dict()
        for name, check in self.checks.items():
            step_metrics[f"{name}"] = check(env)
        return step_metrics

    def _compute_episode_metrics(self, env, episode_info):
        # Compute any collisions from this step
        episode_metrics = dict()
        for name, collisions in episode_info.items():
            collisions = th.tensor(collisions)
            episode_metrics[f"{name}::n_collision"] = collisions.sum().item()

        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            collision_limits=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            collision_limits (None or dict): If specified, should map collision check name (i.e.: the name
                passed in during self.add_check() to corresponding maximum acceptable collision count)

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        if collision_limits is not None:
            for name, collision_limit in collision_limits.items():
                test_name = name
                if collision_limit is not None:
                    n_collisions = episode_metrics[f"{name}::n_collision"]
                    success = n_collisions <= collision_limit
                    feedback = None if success else f"Too many collisions ({n_collisions}) when checking {name}, must be <= {collision_limit}"
                    results[test_name] = {"success": success, "feedback": feedback}

        return results


class TaskSuccessMetric(EnvMetric):

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        # Record whether task is done (terminated is true but not truncated)
        return {"done": terminated and not truncated}

    def _compute_episode_metrics(self, env, episode_info):
        # Derive acceleration -> jerk based on the recorded velocities
        return {"success": th.any(th.tensor(episode_info["done"])).item()}

    @classmethod
    def validate_episode(cls, episode_metrics):
        success = episode_metrics["success"]
        feedback = None if success else "Task was a not a success!"
        return {"task_success": {"success": success, "feedback": feedback}}


class GhostHandAppearanceMetric(EnvMetric):

    @classmethod
    def is_compatible(cls, env):
        valid = super().is_compatible(env=env)
        if valid:
            # We must be using a binary / smooth gripper controller for each robot to ensure that we can
            # infer un/grasping intent
            for robot in env.robots:
                for arm in robot.arm_names:
                    gripper_controller = robot.controllers[f"gripper_{arm}"]
                    is_1d = gripper_controller.command_dim == 1
                    is_normalized = (th.all(cb.to_torch(gripper_controller.command_input_limits[0]) == -1.0).item() and
                                     th.all(cb.to_torch(gripper_controller.command_input_limits[1]) == 1.0).item())
                    valid = is_1d and is_normalized
                    if not valid:
                        break
                if not valid:
                    break
        return valid

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        # Record whether task is done (terminated is true but not truncated)
        step_metrics = dict()
        for i, robot in enumerate(env.robots):
            robot_qpos = robot.get_joint_positions(normalized=False)
            gripper_action_idxs = robot.gripper_action_idx
            for arm in robot.arm_names:
                active = th.max(th.abs(
                        robot_qpos[robot.arm_control_idx[arm]] - action[robot.arm_action_idx[arm]]
                )).item() > GHOST_APPEAR_THRESHOLD
                gripper_controller = robot.controllers[f"gripper_{arm}"]
                step_metrics[f"robot{i}::arm_{arm}::active"] = active
                op = operator.lt if gripper_controller._inverted else operator.ge
                step_metrics[f"robot{i}::arm_{arm}::open_cmd"] = th.all(op(action[gripper_action_idxs[arm]], 0.0)).item()
        return step_metrics

    def _compute_episode_metrics(self, env, episode_info):
        # Aggregate number of steps the ghost hands have appeared per-arm, and whether the robot was releasing a grasp
        # during that time
        episode_metrics = dict()
        for i, robot in enumerate(env.robots):
            for arm in robot.arm_names:
                pf = f"robot{i}::arm_{arm}"
                active = th.tensor(episode_info[f"{pf}::active"])
                open_cmd = th.tensor(episode_info[f"{pf}::open_cmd"])
                ungrasping = open_cmd[1:] & ~open_cmd[:-1]
                episode_metrics[f"{pf}::n_steps_total"] = active.sum().item()
                episode_metrics[f"{pf}::n_steps_while_ungrasping"] = (active[1:] & ungrasping).sum().item()
        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            gh_appearance_limit=None,
            gh_appearance_limit_while_ungrasping=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            gh_appearance_limit (None or int): If specified, maximum acceptable number of steps where the ghost hand
                was triggered in the episode
            gh_appearance_limit_while_ungrasping (None or int): If specified, maximum number of steps where the
                ghost hand was triggered during an ungrasp

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        if gh_appearance_limit is not None or gh_appearance_limit_while_ungrasping is not None:
            for name, metric in episode_metrics.items():
                test_name = name
                if gh_appearance_limit is not None and "::n_steps_total" in name:
                    success = episode_metrics[name] <= gh_appearance_limit
                    limit = gh_appearance_limit
                elif gh_appearance_limit_while_ungrasping is not None and "::n_steps_while_ungrasping" in name:
                    success = episode_metrics[name] <= gh_appearance_limit_while_ungrasping
                    limit = gh_appearance_limit_while_ungrasping
                else:
                    raise ValueError(f"Got invalid metric name: {name}")
                feedback = None if success else f"Too many ghost hand appearances ({episode_metrics[name]}) when checking {name}, must be <= {limit}"
                results[test_name] = {"success": success, "feedback": feedback}

        return results


class ProlongedPauseMetric(MotionMetric):

    def __init__(self, step_dt, vel_threshold=0.001):
        """
        Args:
            step_dt (float): Amount of time between steps, used to differentiate from pos -> vel -> acc -> jerk
            vel_threshold (float): Per-joint vel threshold for determining whether there's any motion occurring
                at a given step
        """
        self.vel_threshold = vel_threshold
        super().__init__(step_dt=step_dt)

    def _compute_episode_metrics(self, env, episode_info):
        # Derive velocities, then count consecutive steps that contain values greater than our threshold
        episode_metrics = dict()
        for pos_key, positions in episode_info.items():
            positions = th.stack(positions, dim=0)
            vels = (positions[1:] - positions[:-1]) / self.step_dt
            in_motions = th.any(th.abs(vels) > self.vel_threshold, dim=-1)
            max_pause_length = 0
            current_pause_length = 0
            for in_motion in in_motions:
                if not in_motion.item():
                    current_pause_length += 1
                    if current_pause_length > max_pause_length:
                        max_pause_length = current_pause_length
                else:
                    current_pause_length = 0
            episode_metrics[f"{pos_key}::max_pause_length"] = max_pause_length

        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            pause_steps_limit=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            pause_steps_limit (None or int): If specified, maximum acceptable number of consecutive steps without
                robot motion

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        if pause_steps_limit is not None:
            for name, metric in episode_metrics.items():
                test_name = name
                success = episode_metrics[name] <= pause_steps_limit
                feedback = None if success else f"Too many consecutive steps ({episode_metrics[name]}) without robot motion, must be <= {pause_steps_limit}"
                results[test_name] = {"success": success, "feedback": feedback}

        return results


class FailedGraspMetric(EnvMetric):

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        step_metrics = dict()
        for i, robot in enumerate(env.robots):
            # Record whether fingers are closed (values ~ 0) -- this implies a failed grasp
            for arm in robot.arm_names:
                step_metrics[f"robot{i}::arm_{arm}::fingers_closed"] = th.allclose(robot.get_joint_positions()[robot.gripper_control_idx[arm]], th.zeros(2), atol=1e-4)
        return step_metrics

    def _compute_episode_metrics(self, env, episode_info):
        # Compute all fingers_closed upticks to get total failed grasp count
        episode_metrics = dict()
        for i, robot in enumerate(env.robots):
            for arm in robot.arm_names:
                pf = f"robot{i}::arm_{arm}"
                fingers_closed = th.tensor(episode_info[f"{pf}::fingers_closed"])
                fingers_closed_transition = fingers_closed[1:] & ~fingers_closed[:-1]
                episode_metrics[f"{pf}::failed_grasp_count"] = fingers_closed_transition.sum().item()

        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            failed_grasp_limit=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            failed_grasp_limit (None or int): If specified, maximum acceptable number of failed (empty) grasps

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        if failed_grasp_limit is not None:
            for name, metric in episode_metrics.items():
                test_name = name
                success = episode_metrics[name] <= failed_grasp_limit
                feedback = None if success else f"Too many ({episode_metrics[name]}) failed grasps, must be <= {failed_grasp_limit}"
                results[test_name] = {"success": success, "feedback": feedback}

        return results


class TaskRelevantObjectVelocityMetric(EnvMetric):
    def __init__(self, step_dt):
        """
        Args:
            step_dt (float): Amount of time between steps, used to differentiate from pos -> vel
        """
        self.step_dt = step_dt

        super().__init__()

    @classmethod
    def is_compatible(cls, env):
        return isinstance(env.task, BehaviorTask)

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        step_metrics = dict()
        for name, bddl_inst in env.task.object_scope.items():
            if bddl_inst.is_system or not bddl_inst.exists or bddl_inst.fixed_base or "agent" in name:
                continue
            step_metrics[f"{name}::pos"] = bddl_inst.get_position_orientation()[0]
        return step_metrics

    def _compute_episode_metrics(self, env, episode_info):
        # Derive acceleration -> jerk based on the recorded velocities
        episode_metrics = dict()
        for pos_key, positions in episode_info.items():
            positions = th.stack(positions, dim=0)
            vels = th.norm(positions[1:] - positions[:-1], dim=-1) / self.step_dt
            episode_metrics[f"{pos_key}::vel_avg"] = vels.mean().item()
            episode_metrics[f"{pos_key}::vel_std"] = vels.std().item()
            episode_metrics[f"{pos_key}::vel_max"] = vels.max().item()

        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            vel_max_limit=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            vel_max_limit (None or float): If specified, maximum velocity that is acceptable for the episode to
                be validated

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        if vel_max_limit is not None:
            for name, metric in episode_metrics.items():
                if f"::vel_max" in name:
                    test_name = name
                    success = metric <= vel_max_limit
                    feedback = None if success else f"{name} is too high({metric}), must be <= {vel_max_limit}"
                    results[test_name] = {"success": success, "feedback": feedback}

        return results


class FieldOfViewMetric(EnvMetric):
    """
    When teleoperator grasp/release, the gripper needs to be in field of view
    """
    @classmethod
    def is_compatible(cls, env):
        valid = super().is_compatible(env=env)
        if valid:
            # We must be using a binary / smooth gripper controller for each robot to ensure that we can
            # infer un/grasping intent
            for robot in env.robots:
                for arm in robot.arm_names:
                    gripper_controller = robot.controllers[f"gripper_{arm}"]
                    is_1d = gripper_controller.command_dim == 1
                    is_normalized = (th.all(cb.to_torch(gripper_controller.command_input_limits[0]) == -1.0).item() and
                                     th.all(cb.to_torch(gripper_controller.command_input_limits[1]) == 1.0).item())
                    valid = is_1d and is_normalized
                    if not valid:
                        break
                if not valid:
                    break
        return valid

    def __init__(self, head_camera, gripper_link_paths):
        """
        Args:
            head_camera (VisionSensor): The head camera of the robot
            gripper_link_paths (dict): The paths of the gripper links
        """
        self.head_camera = head_camera
        self.gripper_link_paths = gripper_link_paths

        assert "seg_instance_id" in self.head_camera.modalities, "FieldOfViewMetric requires instance_id_segmentation modality"

        super().__init__()

    def _compute_step_metrics(self, env, action, obs, reward, terminated, truncated, info):
        step_metrics = dict()
        for i, robot in enumerate(env.robots):
            _, info = self.head_camera.get_obs()
            links_in_fov = set(info["seg_instance_id"].values())
            gripper_action_idxs = robot.gripper_action_idx
            for arm in robot.arm_names:
                gripper_controller = robot.controllers[f"gripper_{arm}"]
                op = operator.lt if gripper_controller._inverted else operator.ge
                # check if any of the gripper link for this arm is in the field of view
                gripper_in_fov = len(links_in_fov.intersection(self.gripper_link_paths[arm])) > 0

                step_metrics[f"robot{i}::arm_{arm}::open_cmd"] = th.all(op(action[gripper_action_idxs[arm]], 0.0)).item()
                step_metrics[f"robot{i}::arm_{arm}::gripper_in_fov"] = gripper_in_fov
        return step_metrics

    def _compute_episode_metrics(self, env, episode_info):
        episode_metrics = dict()

        for i, robot in enumerate(env.robots):
            for arm in robot.arm_names:
                pf = f"robot{i}::arm_{arm}"
                open_cmd = th.tensor(episode_info[f"{pf}::open_cmd"])
                gripper_in_fov = th.tensor(episode_info[f"{pf}::gripper_in_fov"])

                # Detect grasping state changes (comparing current with previous)
                # For index 0, we assume no change (start of episode), so we only evaluate index 1 - end
                grasping_changes = open_cmd[1:] != open_cmd[:-1]

                # Count steps when gripper is not in field of view
                episode_metrics[f"robot{i}::arm_{arm}::gripper_outside_fov"] = (gripper_in_fov == 0).sum().item()

                # Count changes when gripper was NOT in field of view (undesired behavior)
                episode_metrics[f"robot{i}::arm_{arm}::grasp_changes_outside_fov"] = (grasping_changes & ~gripper_in_fov[1:]).sum().item()
        return episode_metrics

    @classmethod
    def validate_episode(
            cls,
            episode_metrics,
            gripper_changes_outside_fov_limit=None,
    ):
        """
        Validates the given @episode_metrics from self.aggregate_results using any specific @kwargs

        Args:
            episode_metrics (dict): Metrics aggregated using self.aggregate_results
            gripper_changes_outside_fov_limit (None or float): If specified, maximum acceptable number of instances
                where the gripper was outside the fov during an un/grasp change

        Returns:
            dict: Keyword-mapped dictionary mapping each validation test name to {"success": bool, "feedback": str} dict
                where "success" is True if the given @episode_metrics pass that specific test; if False, "feedback"
                provides information as to why the test failed
        """
        results = dict()
        if gripper_changes_outside_fov_limit is not None:
            for name, metric in episode_metrics.items():
                if f"::grasp_changes_outside_fov" in name:
                    test_name = name
                    success = metric <= gripper_changes_outside_fov_limit
                    feedback = None if success else f"{name} is too high ({metric}) (too many times the gripper was toggled outside of the robot's main FOV), must be <= {gripper_changes_outside_fov_limit}"
                    results[test_name] = {"success": success, "feedback": feedback}

        return results


def check_robot_self_collision(env):
    # TODO: What about gripper finger self collision?
    for robot in env.robots:
        link_paths = robot.link_prim_paths
        if RigidContactAPI.in_contact(link_paths, link_paths):
            return True
    return False


def check_robot_base_nonarm_nonkinematic_collision(env):
    # TODO: How to check for wall collisions? They're kinematic only
    # # One solution: Make them non-kinematic only during QA checking
    # floor_link_paths = []
    # for structure_category in STRUCTURE_CATEGORIES:
    #     for structure in env.scene.object_registry("category", structure_category):
    #         floor_link_paths += structure.link_prim_paths
    # floor_link_col_idxs = {RigidContactAPI.get_body_col_idx(link_path) for link_path in floor_link_paths}

    for robot in env.robots:
        robot_link_paths = set(robot.link_prim_paths)
        for arm in robot.arm_names:
            robot_link_paths -= set(link.prim_path for link in robot.arm_links[arm])
            robot_link_paths -= set(link.prim_path for link in robot.gripper_links[arm])
            robot_link_paths -= set(link.prim_path for link in robot.finger_links[arm])
    robot_link_idxs = [RigidContactAPI.get_body_col_idx(link_path)[1] for link_path in robot_link_paths]
    robot_contacts = RigidContactAPI.get_all_impulses(env.scene.idx)[robot_link_idxs]

    return th.any(robot_contacts).item()


def create_collision_metric(include_robot_self_collision=True, include_robot_nonarm_nonkinematic_collision=True):
    col_metric = CollisionMetric()
    if include_robot_self_collision:
        col_metric.add_check(name="robot_self", check=check_robot_self_collision)
    if include_robot_nonarm_nonkinematic_collision:
        col_metric.add_check(name="robot_nonarm_nonstructure", check=check_robot_base_nonarm_nonkinematic_collision)
    return col_metric

