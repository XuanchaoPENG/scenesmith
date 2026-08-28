"""Deterministic single-table acquisition for desktop scenes."""

import json
import logging

from dataclasses import dataclass

from scenesmith.agent_utils.asset_manager import AssetGenerationRequest
from scenesmith.agent_utils.room import (
    ObjectType,
    RoomScene,
    copy_scene_object_with_new_pose,
)
from scenesmith.furniture_agents.base_furniture_agent import BaseFurnitureAgent
from scenesmith.prompts import prompt_registry
from scenesmith.prompts.registry import DesktopAgentPrompts

console_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesktopSceneSpec:
    """Structured interpretation of a desktop-scene prompt."""

    table_description: str
    table_short_name: str
    table_dimensions_m: tuple[float, float, float]
    required_items: tuple[str, ...]
    optional_items: tuple[str, ...]
    style_notes: str
    layout_notes: str

    @classmethod
    def fallback(cls, cfg, scene_description: str) -> "DesktopSceneSpec":
        dims = tuple(float(v) for v in cfg.desktop.table.default_dimensions_m)
        return cls(
            table_description=str(cfg.desktop.table.description),
            table_short_name="desktop_table",
            table_dimensions_m=dims,
            required_items=(scene_description,),
            optional_items=(),
            style_notes="Follow the prompt and reference image closely.",
            layout_notes=(
                "Use only the main tabletop. Preserve the spatial and stacking "
                "relationships described by the prompt/reference."
            ),
        )

    @classmethod
    def from_json(cls, payload: dict, cfg) -> "DesktopSceneSpec":
        fallback_dims = [float(v) for v in cfg.desktop.table.default_dimensions_m]
        raw_dims = payload.get("table_dimensions_m", fallback_dims)
        if not isinstance(raw_dims, list) or len(raw_dims) != 3:
            raw_dims = fallback_dims
        dims = tuple(float(v) for v in raw_dims)
        if any(v <= 0 for v in dims):
            dims = tuple(fallback_dims)

        required = payload.get("required_items", [])
        optional = payload.get("optional_items", [])
        if not isinstance(required, list):
            required = []
        if not isinstance(optional, list):
            optional = []

        return cls(
            table_description=str(
                payload.get("table_description") or cfg.desktop.table.description
            ),
            table_short_name=str(payload.get("table_short_name") or "desktop_table"),
            table_dimensions_m=dims,
            required_items=tuple(str(item) for item in required if str(item).strip()),
            optional_items=tuple(str(item) for item in optional if str(item).strip()),
            style_notes=str(payload.get("style_notes") or "natural desktop still life"),
            layout_notes=str(
                payload.get("layout_notes")
                or "Keep every object on the main tabletop and preserve usable space."
            ),
        )


class DesktopFurnitureAgent(BaseFurnitureAgent):
    """Generate exactly one table without invoking room-layout furniture planning."""

    async def _analyze_prompt(self, scene_description: str) -> DesktopSceneSpec:
        prompt = prompt_registry.get_prompt(
            DesktopAgentPrompts.ANALYZE_DESKTOP_PROMPT,
            scene_description=scene_description,
            default_table_description=str(self.cfg.desktop.table.description),
            default_table_dimensions=list(self.cfg.desktop.table.default_dimensions_m),
        )
        retries = int(self.cfg.desktop.prompt_analysis.max_retries)
        reasoning_cfg = self.cfg.openai.reasoning_effort
        verbosity_cfg = self.cfg.openai.verbosity
        reasoning_effort = reasoning_cfg.get(
            "furniture_analysis", reasoning_cfg.get("asset_analysis", "medium")
        )
        verbosity = verbosity_cfg.get(
            "furniture_analysis", verbosity_cfg.get("asset_analysis", "medium")
        )
        for attempt in range(retries):
            try:
                response = self.vlm_service.create_completion(
                    model=self.cfg.openai.model,
                    messages=[{"role": "user", "content": prompt}],
                    reasoning_effort=reasoning_effort,
                    verbosity=verbosity,
                    response_format={"type": "json_object"},
                    vision_detail=self.cfg.openai.vision_detail,
                )
                return DesktopSceneSpec.from_json(json.loads(response), self.cfg)
            except Exception as exc:
                console_logger.warning(
                    "Desktop prompt analysis attempt %d/%d failed: %s",
                    attempt + 1,
                    retries,
                    exc,
                )
        console_logger.warning("Using fallback desktop scene specification")
        return DesktopSceneSpec.fallback(self.cfg, scene_description)

    async def add_furniture(self, scene: RoomScene) -> None:
        """Analyze the prompt, generate one table, and place it at the origin."""
        self.scene = scene
        spec = await self._analyze_prompt(scene.text_description)
        request = AssetGenerationRequest(
            object_descriptions=[spec.table_description],
            short_names=[spec.table_short_name],
            object_type=ObjectType.FURNITURE,
            desired_dimensions=[list(spec.table_dimensions_m)],
            style_context=spec.style_notes,
            scene_id=scene.scene_dir.name,
        )
        result = self.asset_manager.generate_assets(request)
        if not result.successful_assets:
            failures = "; ".join(f.error_message for f in result.failed_assets)
            raise RuntimeError(f"Desktop table generation failed: {failures}")

        # The request contains one table. If a router/backend unexpectedly returns
        # multiple candidates, keep only the largest footprint deterministically.
        table_asset = max(
            result.successful_assets,
            key=lambda obj: (
                float(
                    (obj.bbox_max[0] - obj.bbox_min[0])
                    * (obj.bbox_max[1] - obj.bbox_min[1])
                )
                if obj.bbox_min is not None and obj.bbox_max is not None
                else 0.0
            ),
        )
        table = copy_scene_object_with_new_pose(
            scene=scene,
            original=table_asset,
            x=0.0,
            y=0.0,
            z=0.0,
            yaw=0.0,
        )
        table.metadata.update(
            {
                "desktop_target": True,
                "desktop_required_items": json.dumps(
                    list(spec.required_items), ensure_ascii=False
                ),
                "desktop_optional_items": json.dumps(
                    list(spec.optional_items), ensure_ascii=False
                ),
                "desktop_style_notes": spec.style_notes,
                "desktop_layout_notes": spec.layout_notes,
            }
        )
        scene.add_object(table)
        console_logger.info(
            "Created desktop target %s with dimensions %s",
            table.object_id,
            spec.table_dimensions_m,
        )
