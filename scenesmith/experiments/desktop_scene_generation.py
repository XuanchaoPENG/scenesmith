"""Focused generation pipeline for one table and objects on its main surface."""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET

from datetime import timedelta
from pathlib import Path

import numpy as np

from agents import trace
from pydrake.all import RigidTransform

from scenesmith.agent_utils.house import RoomGeometry
from scenesmith.agent_utils.physical_feasibility import (
    apply_physical_feasibility_postprocessing,
)
from scenesmith.agent_utils.room import ObjectType, RoomScene, SceneObject, UniqueID
from scenesmith.desktop_agents import (
    DesktopFurnitureAgent,
    DesktopManipulandAgent,
    validate_desktop_scene,
)
from scenesmith.experiments.base_experiment import BaseExperiment
from scenesmith.experiments.indoor_scene_generation import (
    IndoorSceneGenerationExperiment,
    RenderGPUAllocator,
    _export_scene_blend_file,
    _load_prompts_from_csv,
    _reset_inherited_sdk_state,
)
from scenesmith.utils.logging import ConsoleLogger, FileLoggingContext
from scenesmith.utils.parallel import run_parallel_isolated

console_logger = logging.getLogger(__name__)


def configure_desktop_agent_configs(cfg_dict: dict) -> dict:
    """Apply the shared desktop policy before constructing stage agents."""
    desktop_cfg = cfg_dict["experiment"]["desktop"]
    cfg_dict["furniture_agent"]["desktop"] = desktop_cfg
    cfg_dict["manipuland_agent"]["desktop"] = desktop_cfg
    cfg_dict["manipuland_agent"]["context_furniture"]["enabled"] = False
    if desktop_cfg["objects"].get("atomic_asset_requests", True):
        # The general router is allowed to split composites (for example a bottle
        # and its visible liquid). Desktop prompts specify exact object counts, so
        # preserve the designer's one-request/one-asset contract.
        cfg_dict["manipuland_agent"]["asset_manager"]["router"]["enabled"] = False
    return desktop_cfg


def create_minimal_desktop_studio(
    scene_dir: Path,
    *,
    width_m: float = 4.0,
    length_m: float = 4.0,
    floor_thickness_m: float = 0.1,
) -> RoomGeometry:
    """Create a wall-free physical studio required by ``RoomScene`` and Drake.

    The floor is kept in room geometry (not ``scene.objects``), so desktop-only
    validation and object exports remain limited to the table and its objects.
    """
    studio_dir = scene_dir / "studio_geometry"
    studio_dir.mkdir(parents=True, exist_ok=True)
    sdf_path = studio_dir / "desktop_studio.sdf"

    sdf = ET.Element("sdf", {"version": "1.7"})
    model = ET.SubElement(sdf, "model", {"name": "room_geometry"})
    link = ET.SubElement(model, "link", {"name": "room_geometry_body_link"})
    floor_pose = f"0 0 {-floor_thickness_m / 2.0} 0 0 0"
    floor_size = f"{length_m} {width_m} {floor_thickness_m}"

    for tag in ("visual", "collision"):
        element = ET.SubElement(link, tag, {"name": f"floor_{tag}"})
        ET.SubElement(element, "pose").text = floor_pose
        geometry = ET.SubElement(element, "geometry")
        box = ET.SubElement(geometry, "box")
        ET.SubElement(box, "size").text = floor_size
        if tag == "visual":
            material = ET.SubElement(element, "material")
            diffuse = ET.SubElement(material, "diffuse")
            diffuse.text = "0.82 0.80 0.76 1"

    tree = ET.ElementTree(sdf)
    tree.write(sdf_path, encoding="utf-8", xml_declaration=True)

    floor = SceneObject(
        object_id=UniqueID("desktop_studio_floor"),
        object_type=ObjectType.FLOOR,
        name="desktop studio floor",
        description="Neutral hidden support floor for desktop-scene physics",
        transform=RigidTransform.Identity(),
        bbox_min=np.array([-length_m / 2, -width_m / 2, -floor_thickness_m]),
        bbox_max=np.array([length_m / 2, width_m / 2, 0.0]),
        immutable=True,
    )
    return RoomGeometry(
        sdf_tree=tree,
        sdf_path=sdf_path,
        walls=[],
        floor=floor,
        wall_normals={},
        width=width_m,
        length=length_m,
        wall_height=3.0,
        wall_thickness=0.0,
        openings=[],
    )


class DesktopSceneGenerationExperiment(IndoorSceneGenerationExperiment):
    """Generate focused desktop scenes while reusing SceneSmith asset services."""

    compatible_furniture_agents = {
        "stateful_furniture_agent": DesktopFurnitureAgent,
        "desktop_furniture_agent": DesktopFurnitureAgent,
    }
    compatible_manipuland_agents = {
        "stateful_manipuland_agent": DesktopManipulandAgent,
        "desktop_manipuland_agent": DesktopManipulandAgent,
    }

    @staticmethod
    def _generate_single_scene(
        prompt: str,
        scene_id: int,
        output_dir: Path,
        cfg_dict: dict,
        capture_logs: bool = False,
        experiment_run_id: str | None = None,
        render_gpu_id: int | None = None,
    ) -> None:
        _reset_inherited_sdk_state()
        started_at = time.time()
        scene_dir = output_dir / f"scene_{scene_id:03d}"
        scene_dir.mkdir(parents=True, exist_ok=True)
        log_path = scene_dir / "scene.log"
        logger = ConsoleLogger(output_dir=scene_dir)

        # Agent builders receive only their agent-specific config. Inject the shared
        # desktop policy into both without changing the normal indoor configs.
        desktop_cfg = configure_desktop_agent_configs(cfg_dict)

        with FileLoggingContext(log_file_path=log_path, suppress_stdout=capture_logs):
            trace_metadata = {
                "scene_id": f"scene_{scene_id:03d}",
                "experiment_name": cfg_dict["name"],
                "scene_dir": str(scene_dir),
                "prompt": prompt,
                "mode": "desktop",
            }
            if experiment_run_id:
                trace_metadata["experiment_run_id"] = experiment_run_id

            with trace(
                workflow_name=f"desktop_scene_{scene_id:03d}_generation",
                metadata=trace_metadata,
            ):
                studio_cfg = desktop_cfg["studio"]
                room_geometry = create_minimal_desktop_studio(
                    scene_dir,
                    width_m=float(studio_cfg["width_m"]),
                    length_m=float(studio_cfg["length_m"]),
                    floor_thickness_m=float(studio_cfg["floor_thickness_m"]),
                )
                scene = RoomScene(
                    room_geometry=room_geometry,
                    scene_dir=scene_dir,
                    room_id="desktop",
                    room_type="desktop",
                    text_description=prompt,
                    action_log_path=scene_dir / "action_log.json",
                )

                furniture_agent = BaseExperiment.build_furniture_agent(
                    cfg_dict=cfg_dict,
                    compatible_agents=(
                        DesktopSceneGenerationExperiment.compatible_furniture_agents
                    ),
                    logger=logger,
                    render_gpu_id=render_gpu_id,
                )
                try:
                    asyncio.run(furniture_agent.add_furniture(scene))
                finally:
                    furniture_agent.cleanup()
                logger.log_scene(scene=scene, name="desktop_after_table")

                manipuland_agent = BaseExperiment.build_manipuland_agent(
                    cfg_dict=cfg_dict,
                    compatible_agents=(
                        DesktopSceneGenerationExperiment.compatible_manipuland_agents
                    ),
                    logger=logger,
                    render_gpu_id=render_gpu_id,
                )
                try:
                    asyncio.run(manipuland_agent.add_manipulands(scene))
                finally:
                    manipuland_agent.cleanup()

                projection_cfg = cfg_dict["experiment"]["projection"]
                if projection_cfg["enabled"] and projection_cfg["final"]["enabled"]:
                    final_cfg = projection_cfg["final"]
                    sim_cfg = projection_cfg["simulation"]
                    logger.log_scene(scene=scene, name="desktop_pre_projection")
                    physics_cfg = cfg_dict["manipuland_agent"]["physics_validation"]
                    scene, success, removed_ids = (
                        apply_physical_feasibility_postprocessing(
                            scene=scene,
                            weld_furniture=True,
                            projection_enabled=True,
                            projection_influence_distance=final_cfg[
                                "influence_distance"
                            ],
                            projection_solver_name=final_cfg["solver_name"],
                            projection_iteration_limit=final_cfg["iteration_limit"],
                            projection_time_limit_s=final_cfg["time_limit_s"],
                            projection_xy_only=final_cfg["xy_only"],
                            projection_fix_rotation=final_cfg["fix_rotation"],
                            simulation_enabled=sim_cfg["enabled"],
                            simulation_time_s=sim_cfg["simulation_time_s"],
                            simulation_time_step_s=sim_cfg["time_step_s"],
                            simulation_timeout_s=sim_cfg["timeout_s"],
                            remove_fallen_furniture=False,
                            remove_fallen_manipulands=physics_cfg[
                                "remove_fallen_manipulands"
                            ],
                            fallen_manipuland_floor_z=physics_cfg[
                                "fallen_manipuland_floor_z"
                            ],
                            fallen_manipuland_near_floor_z=physics_cfg[
                                "fallen_manipuland_near_floor_z"
                            ],
                            fallen_manipuland_z_displacement=physics_cfg[
                                "fallen_manipuland_z_displacement"
                            ],
                        )
                    )
                    if not success:
                        console_logger.warning(
                            "Desktop final projection did not converge"
                        )
                    if removed_ids:
                        console_logger.warning(
                            "Removed fallen desktop objects: %s", removed_ids
                        )

                validation = validate_desktop_scene(scene)
                validation_path = scene_dir / "desktop_validation.json"
                validation.save(validation_path)
                logger.log_scene(scene=scene, name="final_desktop_scene")
                _export_scene_blend_file(
                    scene=scene,
                    scene_dir=scene_dir,
                    cfg_dict=cfg_dict,
                    name="final_desktop_scene",
                )
                if not validation.valid:
                    raise RuntimeError(
                        "Desktop scene validation failed: "
                        + "; ".join(validation.errors)
                    )

        console_logger.info(
            "Desktop scene %03d completed in %s",
            scene_id,
            timedelta(seconds=time.time() - started_at),
        )

    def _run_parallel_generation(
        self,
        prompts_with_ids: list[tuple[int, str]],
        cfg_dict: dict,
        experiment_run_id: str,
        num_workers: int,
    ) -> None:
        allocator = RenderGPUAllocator()
        tasks = []
        for scene_id, prompt in prompts_with_ids:
            tasks.append(
                (
                    f"scene_{scene_id:03d}",
                    DesktopSceneGenerationExperiment._generate_single_scene,
                    {
                        "prompt": prompt,
                        "scene_id": scene_id,
                        "output_dir": self.output_dir,
                        "cfg_dict": cfg_dict,
                        "capture_logs": True,
                        "experiment_run_id": experiment_run_id,
                        "render_gpu_id": allocator.allocate(),
                    },
                )
            )
        results = run_parallel_isolated(tasks=tasks, max_workers=num_workers)
        failures = {
            task_id: error
            for task_id, (success, error) in results.items()
            if not success
        }
        if failures:
            raise RuntimeError(f"Desktop scene generation failures: {failures}")

    def generate_scenes(self) -> None:
        """Use the shared reference-conditioning and server lifecycle."""
        super().generate_scenes()

    def evaluate_scenes(self) -> None:
        raise NotImplementedError
