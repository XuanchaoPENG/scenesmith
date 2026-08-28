"""Manipuland agent constrained to one table and one primary tabletop."""

import json
import logging

from scenesmith.agent_utils.room import ObjectType, RoomScene
from scenesmith.agent_utils.scene_analyzer import FurnitureSelection
from scenesmith.manipuland_agents.stateful_manipuland_agent import (
    StatefulManipulandAgent,
)

from .surface_selection import select_primary_desktop_surface

console_logger = logging.getLogger(__name__)


class DesktopManipulandAgent(StatefulManipulandAgent):
    """Reuse the full manipuland workflow while exposing only the main tabletop."""

    def _get_desktop(self, scene: RoomScene):
        candidates = [
            obj
            for obj in scene.objects.values()
            if obj.object_type == ObjectType.FURNITURE
            and bool(obj.metadata.get("desktop_target", False))
        ]
        if len(candidates) != 1:
            raise ValueError(
                "Desktop mode requires exactly one furniture object marked "
                f"desktop_target; found {len(candidates)}"
            )
        return candidates[0]

    async def _analyze_furniture_for_placement(
        self, scene: RoomScene
    ) -> list[FurnitureSelection]:
        table = self._get_desktop(scene)
        required = json.loads(str(table.metadata.get("desktop_required_items", "[]")))
        optional = json.loads(str(table.metadata.get("desktop_optional_items", "[]")))
        required_text = (
            ", ".join(required) if required else "all prompt-specified items"
        )
        optional_text = ", ".join(optional) if optional else "none"
        return [
            FurnitureSelection(
                furniture_id=table.object_id,
                suggested_items=f"REQUIRED: {required_text}. Optional: {optional_text}",
                prompt_constraints=(
                    "Desktop-only mode: place every object on the one allowed main "
                    "tabletop. Do not place anything on the floor, shelves, drawers, "
                    "or secondary surfaces. Treat a sealed container and its visible "
                    "contents (for example, cola inside a bottle) as ONE combined "
                    "asset; never generate the contents as a separate object. Treat "
                    "a background cloth as the tabletop finish unless the request "
                    "explicitly requires a removable cloth object. "
                    + str(table.metadata.get("desktop_layout_notes", ""))
                ),
                style_notes=str(
                    table.metadata.get("desktop_style_notes", "natural desktop scene")
                ),
            )
        ]

    def _setup_furniture_context(self, furniture_selection: FurnitureSelection) -> None:
        table = self.scene.get_object(furniture_selection.furniture_id)
        if table is None or not table.support_surfaces:
            raise ValueError("Desktop target has no extracted support surfaces")

        cfg = self.cfg.desktop.target_surface
        primary = select_primary_desktop_surface(
            table.support_surfaces,
            min_area_m2=float(cfg.min_area_m2),
            max_tilt_degrees=float(cfg.max_tilt_degrees),
            top_height_tolerance_m=float(cfg.top_height_tolerance_m),
        )
        table.support_surfaces = [primary]
        table.metadata["desktop_surface_id"] = str(primary.surface_id)
        console_logger.info(
            "Locked desktop placement to surface %s (area %.3fm2, z %.3fm)",
            primary.surface_id,
            primary.area,
            float(primary.transform.translation()[2]),
        )
        super()._setup_furniture_context(furniture_selection)
