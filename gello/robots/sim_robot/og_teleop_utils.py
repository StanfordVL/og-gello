import os
import yaml
import torch as th
import numpy as np

import omnigibson as og
import omnigibson.lazy as lazy
from omnigibson.macros import gm
from omnigibson.prims import VisualGeomPrim
from omnigibson.prims.material_prim import MaterialPrim
from omnigibson.utils.usd_utils import create_primitive_mesh, absolute_prim_path_to_scene_relative
from omnigibson.utils.ui_utils import dock_window
from omnigibson.utils import transform_utils as T
from omnigibson.sensors import VisionSensor
from omnigibson.objects.usd_object import USDObject

from gello.robots.sim_robot.og_teleop_cfg import *

def infer_trunk_translate_from_torso_qpos(qpos):
    """
    Convert from torso joint positions to trunk translate value
    
    Args:
        qpos (torch.Tensor): Torso joint positions
        
    Returns:
        float: Trunk translate value
    """
    if qpos[0] > R1_DOWNWARD_TORSO_JOINT_POS[0]:
        # This is the interpolation between downward and ground
        translate = 1 + (qpos[0] - R1_DOWNWARD_TORSO_JOINT_POS[0]) / (
                    R1_GROUND_TORSO_JOINT_POS[0] - R1_DOWNWARD_TORSO_JOINT_POS[0])
    else:
        # This is the interpolation between upright and downward
        translate = (qpos[0] - R1_UPRIGHT_TORSO_JOINT_POS[0]) / (
                    R1_DOWNWARD_TORSO_JOINT_POS[0] - R1_UPRIGHT_TORSO_JOINT_POS[0])

    return translate.item()


def infer_torso_qpos_from_trunk_translate(translate):
    """
    Convert from trunk translate value to torso joint positions
    
    Args:
        translate (float): Trunk translate value between 0.0 and 2.0
        
    Returns:
        torch.Tensor: Torso joint positions
    """
    translate = min(max(translate, 0.0), 2.0)

    # Interpolate between the three pre-determined joint positions
    if translate <= 1.0:
        # Interpolate between upright and down positions
        interpolation_factor = translate
        interpolated_trunk_pos = (1 - interpolation_factor) * R1_UPRIGHT_TORSO_JOINT_POS + \
                                 interpolation_factor * R1_DOWNWARD_TORSO_JOINT_POS
    else:
        # Interpolate between down and ground positions
        interpolation_factor = translate - 1.0
        interpolated_trunk_pos = (1 - interpolation_factor) * R1_DOWNWARD_TORSO_JOINT_POS + \
                                 interpolation_factor * R1_GROUND_TORSO_JOINT_POS

    return interpolated_trunk_pos


def print_color(*args, color=None, attrs=(), **kwargs):
    """
    Print text with color in the terminal
    
    Args:
        *args: Arguments to print
        color (str): Color name (red, green, blue, etc.)
        attrs (tuple): Additional attributes (bold, underline, etc.)
        **kwargs: Keyword arguments for print
    """
    import termcolor

    if len(args) > 0:
        args = tuple(termcolor.colored(arg, color=color, attrs=attrs) for arg in args)
    print(*args, **kwargs)


def get_camera_config(name, relative_prim_path, position, orientation, resolution):
    """
    Generate a camera configuration dictionary
    
    Args:
        name (str): Camera name
        relative_prim_path (str): Relative path to camera in the scene
        position (List[float]): Camera position [x, y, z]
        orientation (List[float]): Camera orientation [x, y, z, w]
        resolution (List[int]): Camera resolution [height, width]
        
    Returns:
        dict: Camera configuration dictionary
    """
    return {
        "sensor_type": "VisionSensor",
        "name": name,
        "relative_prim_path": relative_prim_path,
        "modalities": [],
        "sensor_kwargs": {
            "viewport_name": "Viewport",
            "image_height": resolution[0],
            "image_width": resolution[1],
        },
        "position": position,
        "orientation": orientation,
        "pose_frame": "parent",
        "include_in_obs": False,
    }


def create_and_dock_viewport(parent_window, position, ratio, camera_path):
    """
    Create and configure a viewport window.
    
    Args:
        parent_window: Parent window to dock this viewport to
        position: Docking position (LEFT, RIGHT, BOTTOM, etc.)
        ratio: Size ratio for the docked window
        camera_path: Path to the camera to set as active
        
    Returns:
        The created viewport window
    """
    viewport = lazy.omni.kit.viewport.utility.create_viewport_window()
    og.sim.render()
    
    dock_window(
        space=lazy.omni.ui.Workspace.get_window(parent_window),
        name=viewport.name,
        location=position,
        ratio=ratio,
    )
    og.sim.render()
    
    viewport.viewport_api.set_active_camera(camera_path)
    og.sim.render()
    
    return viewport


def setup_cameras(robot, external_sensors, resolution):
    """
    Set up all cameras for teleop visualization
    
    Args:
        robot: The robot object
        external_sensors: External camera sensors
        resolution: Camera resolution [height, width]
        
    Returns:
        tuple: (camera_paths, viewports)
    """
    viewports = {}
    
    if VIEWING_MODE == ViewingMode.MULTI_VIEW_1:
        viewport_left_shoulder = create_and_dock_viewport(
            "DockSpace", 
            lazy.omni.ui.DockPosition.LEFT,
            0.25,
            external_sensors["external_sensor1"].prim_path
        )
        viewport_left_wrist = create_and_dock_viewport(
            viewport_left_shoulder.name,
            lazy.omni.ui.DockPosition.BOTTOM,
            0.5,
            f"{robot.links['left_eef_link'].prim_path}/Camera"
        )
        viewport_right_shoulder = create_and_dock_viewport(
            "DockSpace",
            lazy.omni.ui.DockPosition.RIGHT,
            0.2,
            external_sensors["external_sensor2"].prim_path
        )
        viewport_right_wrist = create_and_dock_viewport(
            viewport_right_shoulder.name,
            lazy.omni.ui.DockPosition.BOTTOM,
            0.5,
            f"{robot.links['right_eef_link'].prim_path}/Camera"
        )
        # Set resolution for all viewports
        for viewport in [viewport_left_shoulder, viewport_left_wrist, 
                        viewport_right_shoulder, viewport_right_wrist]:
            viewport.viewport_api.set_texture_resolution((256, 256))
            og.sim.render()
            
        viewports = {
            "left_shoulder": viewport_left_shoulder,
            "left_wrist": viewport_left_wrist,
            "right_shoulder": viewport_right_shoulder,
            "right_wrist": viewport_right_wrist
        }
            
        for _ in range(3):
            og.sim.render()

    # Setup main camera view
    eyes_cam_prim_path = f"{robot.links['eyes'].prim_path}/Camera"
    og.sim.viewer_camera.active_camera_path = eyes_cam_prim_path
    og.sim.viewer_camera.image_height = resolution[0]
    og.sim.viewer_camera.image_width = resolution[1]

    # Adjust wrist cameras
    left_wrist_camera_prim = lazy.isaacsim.core.utils.prims.get_prim_at_path(
        prim_path=f"{robot.links['left_eef_link'].prim_path}/Camera"
    )
    right_wrist_camera_prim = lazy.isaacsim.core.utils.prims.get_prim_at_path(
        prim_path=f"{robot.links['right_eef_link'].prim_path}/Camera"
    )
    
    left_wrist_camera_prim.GetAttribute("xformOp:translate").Set(
        lazy.pxr.Gf.Vec3d(*R1_WRIST_CAMERA_LOCAL_POS.tolist())
    )
    right_wrist_camera_prim.GetAttribute("xformOp:translate").Set(
        lazy.pxr.Gf.Vec3d(*R1_WRIST_CAMERA_LOCAL_POS.tolist())
    )
    
    left_wrist_camera_prim.GetAttribute("xformOp:orient").Set(
        lazy.pxr.Gf.Quatd(*R1_WRIST_CAMERA_LOCAL_ORI[[3, 0, 1, 2]].tolist())
    ) # expects (w, x, y, z)
    right_wrist_camera_prim.GetAttribute("xformOp:orient").Set(
        lazy.pxr.Gf.Quatd(*R1_WRIST_CAMERA_LOCAL_ORI[[3, 0, 1, 2]].tolist())
    ) # expects (w, x, y, z)

    camera_paths = [
        eyes_cam_prim_path,
        external_sensors["external_sensor0"].prim_path,
    ]
    
    # Lock camera attributes
    LOCK_CAMERA_ATTR = "omni:kit:cameraLock"
    for cam_path in camera_paths:
        cam_prim = lazy.isaacsim.core.utils.prims.get_prim_at_path(cam_path)
        cam_prim.GetAttribute("horizontalAperture").Set(40.0)

        # Lock attributes afterwards as well to avoid external modification
        if cam_prim.HasAttribute(LOCK_CAMERA_ATTR):
            attr = cam_prim.GetAttribute(LOCK_CAMERA_ATTR)
        else:
            attr = cam_prim.CreateAttribute(LOCK_CAMERA_ATTR, lazy.pxr.Sdf.ValueTypeNames.Bool)
        attr.Set(True)

    # Disable all render products to save on speed
    for sensor in VisionSensor.SENSORS.values():
        sensor.render_product.hydra_texture.set_updates_enabled(False)
        
    return camera_paths, viewports


def setup_visualizers(robot, scene):
    """
    Set up visualization elements for teleop
    
    Args:
        robot: The robot object
        scene: The scene object
        
    Returns:
        dict: Dictionary of visualization elements
    """
    vis_elements = {
        "eef_cylinder_geoms": {},
        "vis_mats": {},
        "vertical_visualizers": {},
        "reachability_visualizers": {}
    }
    
    # Create materials for visualization cylinders
    for arm in robot.arm_names:
        vis_elements["vis_mats"][arm] = []
        for axis, color in zip(("x", "y", "z"), VIS_GEOM_COLORS[False]):
            mat_prim_path = f"{robot.prim_path}/Looks/vis_cylinder_{arm}_{axis}_mat"
            mat = MaterialPrim(
                relative_prim_path=absolute_prim_path_to_scene_relative(scene, mat_prim_path),
                name=f"{robot.name}:vis_cylinder_{arm}_{axis}_mat",
            )
            mat.load(scene)
            mat.diffuse_color_constant = color
            mat.enable_opacity = False
            mat.opacity_constant = 0.5
            mat.enable_emission = True
            mat.emissive_color = color.tolist()
            mat.emissive_intensity = 10000.0
            vis_elements["vis_mats"][arm].append(mat)

    # Create material for visual sphere
    mat_prim_path = f"{robot.prim_path}/Looks/vis_sphere_mat"
    sphere_mat = MaterialPrim(
        relative_prim_path=absolute_prim_path_to_scene_relative(scene, mat_prim_path),
        name=f"{robot.name}:vis_sphere_mat",
    )
    sphere_color = np.array([252, 173, 76]) / 255.0
    sphere_mat.load(scene)
    sphere_mat.diffuse_color_constant = th.as_tensor(sphere_color)
    sphere_mat.enable_opacity = True
    sphere_mat.opacity_constant = 0.1 if USE_VISUAL_SPHERES else 0.0
    sphere_mat.enable_emission = True
    sphere_mat.emissive_color = np.array(sphere_color)
    sphere_mat.emissive_intensity = 1000.0
    vis_elements["sphere_mat"] = sphere_mat

    # Create material for vertical cylinder
    if USE_VERTICAL_VISUALIZERS:
        mat_prim_path = f"{robot.prim_path}/Looks/vis_vertical_mat"
        vert_mat = MaterialPrim(
            relative_prim_path=absolute_prim_path_to_scene_relative(scene, mat_prim_path),
            name=f"{robot.name}:vis_vertical_mat",
        )
        vert_color = np.array([252, 226, 76]) / 255.0
        vert_mat.load(scene)
        vert_mat.diffuse_color_constant = th.as_tensor(vert_color)
        vert_mat.enable_opacity = True
        vert_mat.opacity_constant = 0.3
        vert_mat.enable_emission = True
        vert_mat.emissive_color = np.array(vert_color)
        vert_mat.emissive_intensity = 10000.0
        vis_elements["vert_mat"] = vert_mat

    # Extract visualization cylinder settings
    vis_geom_width = VIS_CYLINDER_CONFIG["width"]
    vis_geom_lengths = VIS_CYLINDER_CONFIG["lengths"]
    vis_geom_proportion_offsets = VIS_CYLINDER_CONFIG["proportion_offsets"]
    vis_geom_quat_offsets = VIS_CYLINDER_CONFIG["quat_offsets"]

    # Create visualization cylinders for each arm
    for arm in robot.arm_names:
        hand_link = robot.eef_links[arm]
        vis_elements["eef_cylinder_geoms"][arm] = []
        for axis, length, mat, prop_offset, quat_offset in zip(
            ("x", "y", "z"),
            vis_geom_lengths,
            vis_elements["vis_mats"][arm],
            vis_geom_proportion_offsets,
            vis_geom_quat_offsets,
        ):
            vis_prim_path = f"{hand_link.prim_path}/vis_cylinder_{axis}"
            vis_prim = create_primitive_mesh(
                vis_prim_path,
                "Cylinder",
                extents=1.0
            )
            vis_geom = VisualGeomPrim(
                relative_prim_path=absolute_prim_path_to_scene_relative(scene, vis_prim_path),
                name=f"{robot.name}:arm_{arm}:vis_cylinder_{axis}"
            )
            vis_geom.load(scene)

            # Attach a material to this prim
            vis_geom.material = mat

            vis_geom.scale = th.tensor([vis_geom_width, vis_geom_width, length])
            vis_geom.set_position_orientation(
                position=th.tensor([0, 0, length * prop_offset]), 
                orientation=quat_offset, 
                frame="parent"
            )
            vis_elements["eef_cylinder_geoms"][arm].append(vis_geom)

        # Add vis sphere around EEF for reachability
        if USE_VISUAL_SPHERES:
            vis_prim_path = f"{hand_link.prim_path}/vis_sphere"
            vis_prim = create_primitive_mesh(
                vis_prim_path,
                "Sphere",
                extents=1.0
            )
            vis_geom = VisualGeomPrim(
                relative_prim_path=absolute_prim_path_to_scene_relative(scene, vis_prim_path),
                name=f"{robot.name}:arm_{arm}:vis_sphere"
            )
            vis_geom.load(scene)

            # Attach a material to this prim
            sphere_mat.bind(vis_geom.prim_path)

            vis_geom.scale = th.ones(3) * 0.15
            vis_geom.set_position_orientation(
                position=th.zeros(3), 
                orientation=th.tensor([0, 0, 0, 1.0]), 
                frame="parent"
            )

        # Add vertical cylinder at EEF
        if USE_VERTICAL_VISUALIZERS:
            vis_prim_path = f"{hand_link.prim_path}/vis_vertical"
            vis_prim = create_primitive_mesh(
                vis_prim_path,
                "Cylinder",
                extents=1.0
            )
            vis_geom = VisualGeomPrim(
                relative_prim_path=absolute_prim_path_to_scene_relative(scene, vis_prim_path),
                name=f"{robot.name}:arm_{arm}:vis_vertical"
            )
            
            vis_geom.load(scene)

            # Attach a material to this prim
            vis_elements["vert_mat"].bind(vis_geom.prim_path)

            vis_geom.scale = th.tensor([vis_geom_width, vis_geom_width, 2.0])
            vis_geom.set_position_orientation(
                position=th.zeros(3), 
                orientation=th.tensor([0, 0, 0, 1.0]), 
                frame="parent"
            )
            vis_elements["vertical_visualizers"][arm] = vis_geom

    # Create reachability visualizers
    if USE_REACHABILITY_VISUALIZERS:
        # Create a square formation in front of the robot as reachability signal
        torso_link = robot.links["torso_link4"]
        beam_width = REACHABILITY_VISUALIZER_CONFIG["beam_width"]
        square_distance = REACHABILITY_VISUALIZER_CONFIG["square_distance"]
        square_width = REACHABILITY_VISUALIZER_CONFIG["square_width"]
        square_height = REACHABILITY_VISUALIZER_CONFIG["square_height"]
        beam_color = REACHABILITY_VISUALIZER_CONFIG["beam_color"]

        # Create material for beams
        beam_mat_prim_path = f"{robot.prim_path}/Looks/square_beam_mat"
        beam_mat = MaterialPrim(
            relative_prim_path=absolute_prim_path_to_scene_relative(scene, beam_mat_prim_path),
            name=f"{robot.name}:square_beam_mat",
        )
        beam_mat.load(scene)
        beam_mat.diffuse_color_constant = th.as_tensor(beam_color)
        beam_mat.enable_opacity = False
        beam_mat.opacity_constant = 0.5
        beam_mat.enable_emission = True
        beam_mat.emissive_color = np.array(beam_color)
        beam_mat.emissive_intensity = 10000.0
        vis_elements["beam_mat"] = beam_mat

        edges = [
            # name, position, scale, orientation
            ["top", [square_distance, 0, 0.3], [beam_width, beam_width, square_width], [0.0, th.pi/2, th.pi/2]],
            ["bottom", [square_distance, 0, 0.0], [beam_width, beam_width, square_width], [0.0, th.pi/2, th.pi/2]],
            ["left", [square_distance, 0.2, 0.15], [beam_width, beam_width, square_height], [0.0, 0.0, 0.0]],
            ["right", [square_distance, -0.2, 0.15], [beam_width, beam_width, square_height], [0.0, 0.0, 0.0]]
        ]

        for name, position, scale, orientation in edges:
            edge_prim_path = f"{torso_link.prim_path}/square_edge_{name}"
            edge_prim = create_primitive_mesh(
                edge_prim_path,
                "Cylinder",
                extents=1.0
            )
            edge_geom = VisualGeomPrim(
                relative_prim_path=absolute_prim_path_to_scene_relative(scene, edge_prim_path),
                name=f"{robot.name}:square_edge_{name}"
            )
            edge_geom.load(scene)
            beam_mat.bind(edge_geom.prim_path)
            edge_geom.scale = th.tensor(scale)
            edge_geom.set_position_orientation(
                position=th.tensor(position),
                orientation=T.euler2quat(th.tensor(orientation)),
                frame="parent"
            )
            vis_elements["reachability_visualizers"][name] = edge_geom
    
    return vis_elements


def setup_flashlights(robot):
    """
    Set up flashlights on the robot's end effectors
    
    Args:
        robot: The robot object
        
    Returns:
        dict: Dictionary of flashlight objects
    """
    flashlights = {}
    
    for arm in robot.arm_names:
        light_prim = getattr(lazy.pxr.UsdLux, "SphereLight").Define(
            og.sim.stage, 
            f"{robot.links[f'{arm}_eef_link'].prim_path}/flashlight"
        )
        light_prim.GetRadiusAttr().Set(0.01)
        light_prim.GetIntensityAttr().Set(FLASHLIGHT_INTENSITY)
        light_prim.LightAPI().GetNormalizeAttr().Set(True)
        
        light_prim.ClearXformOpOrder()
        translate_op = light_prim.AddTranslateOp()
        translate_op.Set(lazy.pxr.Gf.Vec3d(-0.01, 0, -0.05))
        light_prim.SetXformOpOrder([translate_op])
        
        flashlights[arm] = light_prim
    
    return flashlights


def setup_task_instruction_ui(task_name, env, robot):
    """
    Set up UI for displaying task instructions and goal status
    
    Args:
        task_name (str): Name of the task
        env: Environment object
        robot: Robot object
        
    Returns:
        tuple: (overlay_window, text_labels, bddl_goal_conditions)
    """
    if task_name is None:
        return None, None, None
    
    bddl_goal_conditions = env.task.activity_natural_language_goal_conditions

    # Setup overlay window
    main_viewport = og.sim.viewer_camera._viewport
    main_viewport.dock_tab_bar_visible = False
    og.sim.render()
    
    overlay_window = lazy.omni.ui.Window(
        main_viewport.name,
        width=0,
        height=0,
        flags=lazy.omni.ui.WINDOW_FLAGS_NO_TITLE_BAR |
            lazy.omni.ui.WINDOW_FLAGS_NO_SCROLLBAR |
            lazy.omni.ui.WINDOW_FLAGS_NO_RESIZE
    )
    og.sim.render()

    text_labels = []
    with overlay_window.frame:
        with lazy.omni.ui.ZStack():
            # Bottom layer - transparent spacer
            lazy.omni.ui.Spacer()
            # Text container at top left
            with lazy.omni.ui.VStack(alignment=lazy.omni.ui.Alignment.LEFT_TOP, spacing=0):
                lazy.omni.ui.Spacer(height=UI_SETTINGS["top_margin"])  # Top margin

                # Create labels for each goal condition
                for line in bddl_goal_conditions:
                    with lazy.omni.ui.HStack(height=20):
                        lazy.omni.ui.Spacer(width=UI_SETTINGS["left_margin"])  # Left margin
                        label = lazy.omni.ui.Label(
                            line,
                            alignment=lazy.omni.ui.Alignment.LEFT_CENTER,
                            style={
                                "color": UI_SETTINGS["goal_unsatisfied_color"],  # Red color (ABGR)
                                "font_size": UI_SETTINGS["font_size"],
                                "margin": 0,
                                "padding": 0
                            }
                        )
                        text_labels.append(label)
    
    # Force render to update the overlay
    og.sim.render()
    
    return overlay_window, text_labels, bddl_goal_conditions


def setup_object_beacons(task_relevant_objects, scene):
    """
    Set up visual beacons for task-relevant objects
    
    Args:
        task_relevant_objects (list): List of task-relevant objects
        scene: Scene object
        
    Returns:
        dict: Dictionary of object beacons
    """
    if not task_relevant_objects:
        return {}
    
    # Generate random colors for object highlighting
    random_colors = lazy.omni.replicator.core.random_colours(N=len(task_relevant_objects))[:, :3].tolist()
    object_highlight_colors = [[r/255, g/255, b/255] for r, g, b in random_colors]
    
    object_beacons = {}
    
    for obj, color in zip(task_relevant_objects, object_highlight_colors):
        obj.set_highlight_properties(color=color)
        
        # Create material for beacon
        mat_prim_path = f"{obj.prim_path}/Looks/beacon_cylinder_mat"
        mat = MaterialPrim(
            relative_prim_path=absolute_prim_path_to_scene_relative(scene, mat_prim_path),
            name=f"{obj.name}:beacon_cylinder_mat",
        )
        mat.load(scene)
        mat.diffuse_color_constant = th.tensor(color)
        mat.enable_opacity = False
        mat.opacity_constant = OBJECT_HIGHLIGHT_SPHERE["opacity"]
        mat.enable_emission = True
        mat.emissive_color = color
        mat.emissive_intensity = OBJECT_HIGHLIGHT_SPHERE["emissive_intensity"]

        # Create visual beacon
        vis_prim_path = f"{obj.prim_path}/beacon_cylinder"
        vis_prim = create_primitive_mesh(
            vis_prim_path,
            "Cylinder",
            extents=1.0
        )
        beacon = VisualGeomPrim(
            relative_prim_path=absolute_prim_path_to_scene_relative(scene, vis_prim_path),
            name=f"{obj.name}:beacon_cylinder"
        )
        beacon.load(scene)
        beacon.material = mat
        beacon.scale = th.tensor([0.01, 0.01, BEACON_LENGTH])
        beacon_pos = obj.aabb_center + th.tensor([0.0, 0.0, BEACON_LENGTH/2.0])
        beacon.set_position_orientation(
            position=beacon_pos, 
            orientation=T.euler2quat(th.tensor([0.0, 0.0, 0.0]))
        )

        object_beacons[obj] = beacon
        beacon.visible = False
        
    return object_beacons


def setup_ghost_robot(scene, task_cfg=None):
    """
    Set up a ghost robot for visualization
    
    Args:
        scene: Scene object
        task_cfg: Dictionary of task configuration (optional)
        
    Returns:
        object: Ghost robot object
    """    
    # NOTE: Add ghost robot, but don't register it
    ghost = USDObject(
        name="ghost", 
        usd_path=os.path.join(gm.ASSET_PATH, f"models/{ROBOT_TYPE.lower()}/usd/{ROBOT_TYPE.lower()}.usda"), 
        visual_only=True, 
        position=(task_cfg is not None and task_cfg["robot_start_position"]) or [0.0, 0.0, 0.0]
    )
    scene.add_object(ghost, register=False)
    
    # Set ghost color
    for mat in ghost.materials:
        mat.diffuse_color_constant = th.tensor([0.8, 0.0, 0.0], dtype=th.float32)
    
    # Hide all links initially
    for link in ghost.links.values():
        link.visible = False
        
    return ghost


def optimize_sim_settings():
    """Apply optimized simulation settings for better performance"""
    settings = lazy.carb.settings.get_settings()

    # Use asynchronous rendering for faster performance
    # NOTE: This gets reset EVERY TIME the sim stops / plays!!
    # For some reason, need to turn on, then take one render step, then turn off, and then back on in order to
    # avoid viewport freezing...not sure why
    settings.set_bool("/app/asyncRendering", True)
    og.sim.render()
    settings.set_bool("/app/asyncRendering", False)
    settings.set_bool("/app/asyncRendering", True)
    settings.set_bool("/app/asyncRenderingLowLatency", True)

    # Must ALWAYS be set after sim plays because omni overrides these values
    settings.set("/app/runLoops/main/rateLimitEnabled", False)
    settings.set("/app/runLoops/main/rateLimitUseBusyLoop", False)

    # Use asynchronous rendering for faster performance (repeat to ensure it takes effect)
    settings.set_bool("/app/asyncRendering", True)
    settings.set_bool("/app/asyncRenderingLowLatency", True)
    settings.set_bool("/app/asyncRendering", False)
    settings.set_bool("/app/asyncRenderingLowLatency", False)
    settings.set_bool("/app/asyncRendering", True)
    settings.set_bool("/app/asyncRenderingLowLatency", True)

    # Additional RTX settings
    settings.set_bool("/rtx-transient/dlssg/enabled", True)
    
    # Disable fractional cutout opacity for speed
    # Alternatively, turn this on so that we can use semi-translucent visualizers
    lazy.carb.settings.get_settings().set_bool("/rtx/raytracing/fractionalCutoutOpacity", False)


def update_ghost_robot(ghost, robot, action, ghost_appear_counter):
    """
    Update the ghost robot visualization based on current robot state and action
    
    Args:
        ghost: Ghost robot object
        robot: Robot object
        action: Current action being applied
        ghost_appear_counter: Counter for ghost appearance timing
        
    Returns:
        dict: Updated ghost_appear_counter
    """
    ghost.set_position_orientation(
        position=robot.get_position_orientation(frame="world")[0],
        orientation=robot.get_position_orientation(frame="world")[1],
    )
    for i in range(4):
        ghost.joints[f"torso_joint{i+1}"].set_pos(robot.joints[f"torso_joint{i+1}"].get_state()[0])
    for arm in robot.arm_names:
        for i in range(6):
            ghost.joints[f"{arm}_arm_joint{i+1}"].set_pos(th.clamp(
                action[robot.arm_action_idx[arm]][i],
                min=ghost.joints[f"{arm}_arm_joint{i+1}"].lower_limit,
                max=ghost.joints[f"{arm}_arm_joint{i+1}"].upper_limit
            ))
        for i in range(2):
            ghost.joints[f"{arm}_gripper_axis{i+1}"].set_pos(
                action[robot.gripper_action_idx[arm]][0],
                normalized=True
            )
        # make arm visible if some joint difference is larger than the threshold
        if th.max(th.abs(
            robot.get_joint_positions()[robot.arm_control_idx[arm]] - action[robot.arm_action_idx[arm]]
        )) > GHOST_APPEAR_THRESHOLD:
            ghost_appear_counter[arm] += 1
            if ghost_appear_counter[arm] >= GHOST_APPEAR_TIME:
                for link_name, link in ghost.links.items():
                    if link_name.startswith(arm):
                        link.visible = True
        else:
            ghost_appear_counter[arm] = 0
            for link_name, link in ghost.links.items():
                if link_name.startswith(arm):
                    link.visible = False
    
    return ghost_appear_counter


def update_goal_status(text_labels, goal_status, prev_goal_status, env, recording_path=None):
    """
    Update the UI based on goal status changes
    
    Args:
        text_labels: List of UI text labels
        goal_status: Current goal status
        prev_goal_status: Previous goal status
        env: Environment object
        recording_path: Path to save recordings (optional)
        
    Returns:
        dict: Updated previous goal status
    """
    if text_labels is None:
        return prev_goal_status

    # Check if status has changed
    status_changed = (set(goal_status['satisfied']) != set(prev_goal_status['satisfied']) or
                    set(goal_status['unsatisfied']) != set(prev_goal_status['unsatisfied']))

    if status_changed:
        # Update satisfied goals - make them green
        for idx in goal_status['satisfied']:
            if 0 <= idx < len(text_labels):
                current_style = text_labels[idx].style
                current_style.update({"color": UI_SETTINGS["goal_satisfied_color"]})  # Green (ABGR)
                text_labels[idx].set_style(current_style)

        # Update unsatisfied goals - make them red
        for idx in goal_status['unsatisfied']:
            if 0 <= idx < len(text_labels):
                current_style = text_labels[idx].style
                current_style.update({"color": UI_SETTINGS["goal_unsatisfied_color"]})  # Red (ABGR)
                text_labels[idx].set_style(current_style)
        
        # Update checkpoint if new goals are satisfied
        if AUTO_CHECKPOINTING and len(goal_status['satisfied']) > len(prev_goal_status['satisfied']):
            if recording_path is not None:
                env.update_checkpoint()
                print("Auto recorded checkpoint due to goal status change!")

        # Return the updated status
        return goal_status.copy()
    
    return prev_goal_status


def update_in_hand_status(robot, vis_mats, prev_in_hand_status):
    """
    Update the visualization based on whether objects are in hand
    
    Args:
        robot: Robot object
        vis_mats: Visualization materials dictionary
        prev_in_hand_status: Previous in-hand status
        
    Returns:
        dict: Updated in-hand status
    """
    updated_status = prev_in_hand_status.copy()
    
    # Update the in-hand status of the robot's arms
    for arm in robot.arm_names:
        in_hand = len(robot._find_gripper_raycast_collisions(arm)) != 0
        if in_hand != prev_in_hand_status[arm]:
            updated_status[arm] = in_hand
            for idx, mat in enumerate(vis_mats[arm]):
                mat.diffuse_color_constant = VIS_GEOM_COLORS[in_hand][idx]
    
    return updated_status


def update_grasp_status(robot, eef_cylinder_geoms, prev_grasp_status):
    """
    Update the visualization based on whether robot is grasping
    
    Args:
        robot: Robot object
        eef_cylinder_geoms: End effector cylinder geometries
        prev_grasp_status: Previous grasp status
        
    Returns:
        dict: Updated grasp status
    """
    updated_status = prev_grasp_status.copy()
    
    for arm in robot.arm_names:
        is_grasping = robot.is_grasping(arm) > 0
        if is_grasping != prev_grasp_status[arm]:
            updated_status[arm] = is_grasping
            for cylinder in eef_cylinder_geoms[arm]:
                cylinder.visible = not is_grasping
    
    return updated_status


def update_reachability_visualizers(reachability_visualizers, joint_cmd, prev_base_motion):
    """
    Update the reachability visualizers based on base motion
    
    Args:
        reachability_visualizers: Reachability visualizer objects
        joint_cmd: Joint command dictionary
        prev_base_motion: Previous base motion state
        
    Returns:
        bool: Updated base motion state
    """
    if not USE_REACHABILITY_VISUALIZERS or not reachability_visualizers:
        return prev_base_motion

    # Show visualizers only when there's nonzero base motion
    has_base_motion = th.any(th.abs(joint_cmd["base"]) > 0.0)
    
    if has_base_motion != prev_base_motion:
        for edge in reachability_visualizers.values():
            edge.visible = has_base_motion
    
    return has_base_motion


def update_checkpoint(env, frame_counter, recording_path=None):
    """
    Update checkpoint based on periodic timer
    
    Args:
        env: Environment object
        frame_counter: Current frame counter
        recording_path: Path to save recordings (optional)
        
    Returns:
        int: Updated frame counter
    """
    if not AUTO_CHECKPOINTING:
        return frame_counter
    
    updated_counter = frame_counter + 1
    
    if frame_counter % STEPS_TO_AUTO_CHECKPOINT == 0:
        if recording_path is not None:
            env.update_checkpoint()
            print("Auto recorded checkpoint due to periodic save!")
        updated_counter = 0
    
    return updated_counter


def load_available_tasks():
    """
    Load available tasks from configuration file
    
    Returns:
        dict: Dictionary of available tasks
    """
    # Get directory of current file
    dir_path = os.path.dirname(os.path.abspath(__file__))
    task_cfg_path = os.path.join(dir_path, '..', '..', '..', 'sampled_task', 'available_tasks.yaml')
    
    try:
        with open(task_cfg_path, 'r') as file:
            available_tasks = yaml.safe_load(file)
        return available_tasks
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"Error loading available tasks: {e}")
        return {}


def generate_basic_environment_config(task_name=None, task_cfg=None):
    """
    Generate a basic environment configuration
    
    Args:
        task_name (str): Name of the task (optional)
        task_cfg: Dictionary of task config (optional)
        
    Returns:
        dict: Environment configuration
    """
    cfg = {
        "env": {
            "action_frequency": 30,
            "rendering_frequency": 30,
            "physics_frequency": 120,
            "external_sensors": [
                get_camera_config(
                    name="external_sensor0", 
                    relative_prim_path=f"/controllable__{ROBOT_TYPE.lower()}__{ROBOT_NAME}/base_link/external_sensor0", 
                    position=EXTERNAL_CAMERA_CONFIGS["external_sensor0"]["position"], 
                    orientation=EXTERNAL_CAMERA_CONFIGS["external_sensor0"]["orientation"], 
                    resolution=RESOLUTION
                ),
            ],
        },
    }
    
    if VIEWING_MODE == ViewingMode.MULTI_VIEW_1:
        cfg["env"]["external_sensors"].append(
            get_camera_config(
                name="external_sensor1", 
                relative_prim_path=f"/controllable__{ROBOT_TYPE.lower()}__{ROBOT_NAME}/base_link/external_sensor1", 
                position=EXTERNAL_CAMERA_CONFIGS["external_sensor1"]["position"], 
                orientation=EXTERNAL_CAMERA_CONFIGS["external_sensor1"]["orientation"], 
                resolution=RESOLUTION
            )
        )
        cfg["env"]["external_sensors"].append(
            get_camera_config(
                name="external_sensor2", 
                relative_prim_path=f"/controllable__{ROBOT_TYPE.lower()}__{ROBOT_NAME}/base_link/external_sensor2", 
                position=EXTERNAL_CAMERA_CONFIGS["external_sensor2"]["position"], 
                orientation=EXTERNAL_CAMERA_CONFIGS["external_sensor2"]["orientation"], 
                resolution=RESOLUTION
            )
        )

    if task_name is not None and task_cfg is not None:
        # Load the environment for a particular task
        cfg["scene"] = {
            "type": "InteractiveTraversableScene",
            "scene_model": task_cfg["scene_model"],
            "load_room_types": None,
            "load_room_instances": None,
            "include_robots": False,
        }

        cfg["task"] = {
            "type": "BehaviorTask",
            "activity_name": task_name,
            "activity_definition_id": 0,
            "activity_instance_id": 0,
            "predefined_problem": None,
            "online_object_sampling": False,
            "debug_object_sampling": False,
            "highlight_task_relevant_objects": False,
            "termination_config": {
                "max_steps": 50000,
            },
            "reward_config": {
                "r_potential": 1.0,
            },
        }
    elif FULL_SCENE:
        cfg["scene"] = {
            "type": "InteractiveTraversableScene",
            "scene_model": "Rs_int",
        }
    else:
        # Simple scene with a table
        x_offset = 0.5
        cfg["scene"] = {"type": "Scene"}
        cfg["objects"] = [
            {
                "type": "PrimitiveObject",
                "name": "table",
                "primitive_type": "Cube",
                "fixed_base": True,
                "scale": [0.5, 0.5, 0.3],
                "position": [0.75 + x_offset, 0, 0.65],
                "orientation": [0.0, 0.0, 0.0, 1.0],
            },
            {
                "type": "PrimitiveObject",
                "name": "table2",
                "primitive_type": "Cube",
                "fixed_base": True,
                "scale": [0.5, 0.5, 0.3],
                "position": [0.0, 0.95, 0.65],
                "orientation": [0.0, 0.0, 0.0, 1.0],
                "rgba": [0.0, 1.0, 1.0, 1.0],
            },
            {
                "type": "PrimitiveObject",
                "name": "table3",
                "primitive_type": "Cube",
                "fixed_base": True,
                "scale": [0.5, 0.5, 0.3],
                "position": [-1.0, 0.0, 0.25],
                "orientation": [0.0, 0.0, 0.0, 1.0],
                "rgba": [1.0, 1.0, 0.0, 1.0],
            }
        ]

        if USE_CLOTH:
            obj_cfgs = [{
                "type": "DatasetObject",
                "name": "obj",
                "category": "dishtowel",
                "model": "dtfspn",
                "prim_type": "CLOTH",
                "scale": [2.0, 2.0, 2.0],
                "position": [0.65 + x_offset, 0, 0.95],
                "orientation": [0.0, 0.0, 0, 1.0],
                "abilities": {"cloth": {}},
            }]
        elif USE_FLUID:
            obj_cfgs = [
                {
                    "type": "DatasetObject",
                    "name": "obj",
                    "category": "coffee_cup",
                    "model": "ykuftq",
                    "scale": [1.5] * 3,
                    "position": [0.65 + x_offset, -0.15, 0.85],
                    "orientation": [0.0, 0.0, 0, 1.0],
                },
                {
                    "type": "DatasetObject",
                    "name": "obj1",
                    "category": "coffee_cup",
                    "model": "xjdyon",
                    "scale": [1.1] * 3,
                    "position": [0.65 + x_offset, 0.15, 0.84],
                    "orientation": [0.0, 0.0, 0, 1.0],
                },
            ]
        elif USE_ARTICULATED:
            obj_cfgs = [{
                "type": "DatasetObject",
                "name": "obj",
                "category": "freezer",
                "model": "aayduy",
                "scale": [0.9, 0.9, 0.9],
                "position": [0.65 + x_offset, 0, 0.95],
                "orientation": [0.0, 0.0, 0, 1.0],
            },
            {
                "type": "DatasetObject",
                "name": "obj2",
                "category": "fridge",
                "model": "dxwbae",
                "scale": [0.9, 0.9, 0.9],
                "position": [5.0, 0, 1.0],
                "orientation": [0.0, 0.0, 0, 1.0],
            },
            {
                "type": "DatasetObject",
                "name": "obj3",
                "category": "wardrobe",
                "model": "bhyopq",
                "scale": [0.9, 0.9, 0.9],
                "position": [10.0, 0, 1.0],
                "orientation": [0.0, 0.0, 0, 1.0],
            },
            ]
        else:
            obj_cfgs = [{
                "type": "DatasetObject",
                "name": "obj",
                "category": "crock_pot",
                "model": "xdahvv",
                "scale": [0.9, 0.9, 0.9],
                "position": [0.65 + x_offset, 0, 0.95],
                "orientation": [0.0, 0.0, 0, 1.0],
            }]
        cfg["objects"] += obj_cfgs
    
    return cfg


def generate_robot_config(task_name=None, task_cfg=None):
    """
    Generate robot configuration
    
    Args:
        task_name: Name of the task (optional)
        task_cfg: Dictionary of task config (optional)
        
    Returns:
        dict: Robot configuration
    """
    # Create a copy of the controller config to avoid modifying the original
    controller_config = {k: v.copy() for k, v in R1_CONTROLLER_CONFIG.items()}
    
    robot_config = {
        "type": ROBOT_TYPE,
        "name": ROBOT_NAME,
        "action_normalize": False,
        "controller_config": controller_config,
        "self_collisions": False,
        "obs_modalities": [],
        "position": [0.0, 0.0, 0.0],
        "orientation": [0.0, 0.0, 0.0, 1.0],
        "grasping_mode": "assisted",
        "sensor_config": {
            "VisionSensor": {
                "sensor_kwargs": {
                    "image_height": RESOLUTION[0],
                    "image_width": RESOLUTION[1],
                },
            },
        },
    }
    
    # Override position and orientation for tasks
    if task_name is not None and task_cfg is not None:
        robot_config["position"] = task_cfg["robot_start_position"]
        robot_config["orientation"] = task_cfg["robot_start_orientation"]
    
    # Add reset joint positions
    joint_pos = R1_RESET_JOINT_POS.clone()
    
    # NOTE: Fingers MUST start open, or else generated AG spheres will be spawned incorrectly
    joint_pos[-4:] = 0.05
    
    # Update trunk qpos as well
    joint_pos[6:10] = infer_torso_qpos_from_trunk_translate(DEFAULT_TRUNK_TRANSLATE)
    
    robot_config["reset_joint_pos"] = joint_pos
    
    return robot_config


def apply_omnigibson_macros():
    """Apply global OmniGibson settings"""
    for key, value in OMNIGIBSON_MACROS.items():
        setattr(gm, key, value)