import json

from pathlib import Path

import numpy as np
import pytest

from pydrake.all import RigidTransform, RollPitchYaw

from scenesmith.agent_utils.drake_utils import (
    create_drake_plant_and_scene_graph_from_scene,
)
from scenesmith.agent_utils.room import (
    ObjectType,
    PlacementInfo,
    RoomScene,
    SceneObject,
    SupportSurface,
    UniqueID,
)
from scenesmith.desktop_agents.surface_selection import select_primary_desktop_surface
from scenesmith.desktop_agents.validation import validate_desktop_scene
from scenesmith.experiments.desktop_scene_generation import (
    configure_desktop_agent_configs,
    create_minimal_desktop_studio,
)


def _surface(
    surface_id: str,
    *,
    area_dims: tuple[float, float],
    z: float,
    tilt_degrees: float = 0.0,
) -> SupportSurface:
    width, depth = area_dims
    return SupportSurface(
        surface_id=UniqueID(surface_id),
        bounding_box_min=np.array([-width / 2, -depth / 2, 0.0]),
        bounding_box_max=np.array([width / 2, depth / 2, 0.5]),
        transform=RigidTransform(
            RollPitchYaw(np.radians([tilt_degrees, 0.0, 0.0])), [0.0, 0.0, z]
        ),
    )


def test_primary_desktop_surface_prefers_largest_surface_in_top_band() -> None:
    lower_large = _surface("lower", area_dims=(1.5, 0.8), z=0.60)
    tabletop = _surface("tabletop", area_dims=(1.4, 0.75), z=0.75)
    raised_detail = _surface("detail", area_dims=(0.6, 0.5), z=0.77)

    selected = select_primary_desktop_surface(
        [lower_large, tabletop, raised_detail],
        top_height_tolerance_m=0.05,
    )

    assert selected.surface_id == "tabletop"


def test_primary_desktop_surface_rejects_tilted_surface() -> None:
    tilted = _surface("tilted", area_dims=(1.4, 0.8), z=0.75, tilt_degrees=15)

    with pytest.raises(ValueError, match="No desktop support surface"):
        select_primary_desktop_surface([tilted])


def test_primary_desktop_surface_accepts_small_horizontal_surface() -> None:
    tiny = _surface("tiny", area_dims=(0.1, 0.1), z=0.8)

    selected = select_primary_desktop_surface([tiny])

    assert selected.surface_id == "tiny"


def test_minimal_desktop_studio_has_floor_but_no_walls(tmp_path: Path) -> None:
    geometry = create_minimal_desktop_studio(tmp_path)

    assert geometry.sdf_path.exists()
    assert geometry.walls == []
    assert geometry.floor is not None
    assert geometry.floor.object_type == ObjectType.FLOOR
    assert geometry.floor.immutable
    assert "room_geometry_body_link" in geometry.sdf_path.read_text()
    assert "<roughness>" not in geometry.sdf_path.read_text()

    scene = RoomScene(room_geometry=geometry, scene_dir=tmp_path)
    plant, _ = create_drake_plant_and_scene_graph_from_scene(scene)
    assert plant.num_model_instances() >= 2


def test_desktop_agent_config_preserves_atomic_asset_requests() -> None:
    desktop = {"objects": {"atomic_asset_requests": True}}
    cfg = {
        "experiment": {"desktop": desktop},
        "furniture_agent": {},
        "manipuland_agent": {
            "context_furniture": {"enabled": True},
            "asset_manager": {"router": {"enabled": True}},
        },
    }

    result = configure_desktop_agent_configs(cfg)

    assert result is desktop
    assert cfg["furniture_agent"]["desktop"] is desktop
    assert cfg["manipuland_agent"]["desktop"] is desktop
    assert cfg["manipuland_agent"]["context_furniture"]["enabled"] is False
    assert cfg["manipuland_agent"]["asset_manager"]["router"]["enabled"] is False


def test_desktop_validation_accepts_one_table_and_objects_on_locked_surface(
    tmp_path: Path,
) -> None:
    geometry = create_minimal_desktop_studio(tmp_path)
    scene = RoomScene(room_geometry=geometry, scene_dir=tmp_path)
    tabletop = _surface("S_0", area_dims=(1.4, 0.8), z=0.75)
    table = SceneObject(
        object_id=UniqueID("table_0"),
        object_type=ObjectType.FURNITURE,
        name="table",
        description="table",
        transform=RigidTransform.Identity(),
        support_surfaces=[tabletop],
        bbox_min=np.array([-0.7, -0.4, 0.0]),
        bbox_max=np.array([0.7, 0.4, 0.75]),
        metadata={"desktop_target": True, "desktop_surface_id": "S_0"},
    )
    bottle = SceneObject(
        object_id=UniqueID("bottle_0"),
        object_type=ObjectType.MANIPULAND,
        name="bottle",
        description="glass cola bottle",
        transform=RigidTransform(p=[0.0, 0.0, 0.75]),
        placement_info=PlacementInfo(
            parent_surface_id=UniqueID("S_0"),
            position_2d=np.array([0.0, 0.0]),
            rotation_2d=0.0,
        ),
    )
    scene.add_object(table)
    scene.add_object(bottle)

    result = validate_desktop_scene(scene)

    assert result.valid
    assert result.stats["furniture_count"] == 1
    assert result.stats["tabletop_object_count"] == 1


def test_desktop_validation_rejects_extra_furniture_and_wrong_surface(
    tmp_path: Path,
) -> None:
    geometry = create_minimal_desktop_studio(tmp_path)
    scene = RoomScene(room_geometry=geometry, scene_dir=tmp_path)
    table = SceneObject(
        object_id=UniqueID("table_0"),
        object_type=ObjectType.FURNITURE,
        name="table",
        description="table",
        transform=RigidTransform.Identity(),
        metadata={"desktop_target": True, "desktop_surface_id": "S_0"},
    )
    chair = SceneObject(
        object_id=UniqueID("chair_0"),
        object_type=ObjectType.FURNITURE,
        name="chair",
        description="chair",
        transform=RigidTransform.Identity(),
    )
    misplaced = SceneObject(
        object_id=UniqueID("book_0"),
        object_type=ObjectType.MANIPULAND,
        name="book",
        description="book",
        transform=RigidTransform.Identity(),
        placement_info=PlacementInfo(
            parent_surface_id=UniqueID("S_9"),
            position_2d=np.array([0.0, 0.0]),
            rotation_2d=0.0,
        ),
    )
    scene.add_object(table)
    scene.add_object(chair)
    scene.add_object(misplaced)

    result = validate_desktop_scene(scene)

    assert not result.valid
    assert any("exactly one furniture" in error for error in result.errors)
    assert any("expected S_0" in error for error in result.errors)


def test_desktop_validation_counts_composite_stack_members(tmp_path: Path) -> None:
    geometry = create_minimal_desktop_studio(tmp_path)
    scene = RoomScene(room_geometry=geometry, scene_dir=tmp_path)
    table = SceneObject(
        object_id=UniqueID("table_0"),
        object_type=ObjectType.FURNITURE,
        name="table",
        description="table",
        transform=RigidTransform.Identity(),
        metadata={"desktop_target": True, "desktop_surface_id": "S_0"},
    )
    stack = SceneObject(
        object_id=UniqueID("stack_0"),
        object_type=ObjectType.MANIPULAND,
        name="stack",
        description="four item stack",
        transform=RigidTransform.Identity(),
        metadata={"composite_type": "stack", "num_members": 4},
        placement_info=PlacementInfo(
            parent_surface_id=UniqueID("S_0"),
            position_2d=np.array([0.0, 0.0]),
            rotation_2d=0.0,
        ),
    )
    scene.add_object(table)
    scene.add_object(stack)

    result = validate_desktop_scene(scene)

    assert result.valid
    assert result.stats["tabletop_object_count"] == 4
    assert result.stats["tabletop_scene_object_count"] == 1


def test_desktop_validation_result_can_be_saved(tmp_path: Path) -> None:
    geometry = create_minimal_desktop_studio(tmp_path)
    scene = RoomScene(room_geometry=geometry, scene_dir=tmp_path)
    result = validate_desktop_scene(scene)
    output = tmp_path / "validation.json"

    result.save(output)

    assert json.loads(output.read_text())["valid"] is False
