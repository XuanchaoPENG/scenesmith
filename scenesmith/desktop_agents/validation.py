"""Deterministic structural validation for generated desktop scenes."""

import json

from dataclasses import asdict, dataclass, field
from pathlib import Path

from scenesmith.agent_utils.room import ObjectType, RoomScene


@dataclass
class DesktopValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int | float | str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as output:
            json.dump(self.to_dict(), output, indent=2, ensure_ascii=False)


def validate_desktop_scene(scene: RoomScene) -> DesktopValidationResult:
    """Validate the hard invariants of desktop-only generation."""
    errors: list[str] = []
    warnings: list[str] = []
    tables = [
        obj
        for obj in scene.objects.values()
        if obj.object_type == ObjectType.FURNITURE
        and bool(obj.metadata.get("desktop_target", False))
    ]
    furniture = [
        obj for obj in scene.objects.values() if obj.object_type == ObjectType.FURNITURE
    ]
    if len(tables) != 1:
        errors.append(f"Expected exactly one desktop target, found {len(tables)}")
    if len(furniture) != 1:
        errors.append(f"Expected exactly one furniture object, found {len(furniture)}")

    forbidden_types = {
        ObjectType.WALL,
        ObjectType.WALL_MOUNTED,
        ObjectType.CEILING_MOUNTED,
        ObjectType.FLOOR,
    }
    forbidden = [
        str(obj.object_id)
        for obj in scene.objects.values()
        if obj.object_type in forbidden_types
    ]
    if forbidden:
        errors.append(f"Forbidden scene objects present: {forbidden}")

    surface_id = None
    if tables:
        surface_id = str(tables[0].metadata.get("desktop_surface_id", "")) or None
        if not surface_id:
            errors.append("Desktop target has no locked desktop_surface_id")

    tabletop_objects = [
        obj
        for obj in scene.objects.values()
        if obj.object_type in (ObjectType.MANIPULAND, ObjectType.THIN_COVERING)
    ]
    tabletop_member_count = sum(
        int(obj.metadata.get("num_members", 1)) for obj in tabletop_objects
    )
    if not tabletop_objects:
        errors.append("Desktop contains no tabletop objects")

    for obj in tabletop_objects:
        if obj.placement_info is None:
            errors.append(f"Object {obj.object_id} has no support-surface placement")
            continue
        parent_id = str(obj.placement_info.parent_surface_id)
        if surface_id is not None and parent_id != surface_id:
            errors.append(
                f"Object {obj.object_id} is on surface {parent_id}, expected {surface_id}"
            )

    classified_ids = {obj.object_id for obj in [*furniture, *tabletop_objects]}
    other = [
        str(obj.object_id)
        for obj in scene.objects.values()
        if obj.object_id not in classified_ids
    ]
    if other:
        warnings.append(f"Unclassified objects present: {other}")

    return DesktopValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        stats={
            "scene_object_count": len(scene.objects),
            "furniture_count": len(furniture),
            "tabletop_object_count": tabletop_member_count,
            "tabletop_scene_object_count": len(tabletop_objects),
            "desktop_surface_id": surface_id or "",
        },
    )
