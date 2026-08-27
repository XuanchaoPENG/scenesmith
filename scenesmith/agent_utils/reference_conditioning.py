"""Reference-image conditioning for text-driven scene generation."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import time

from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from PIL import Image
from pydantic import BaseModel, Field

console_logger = logging.getLogger(__name__)


class ReferenceObject(BaseModel):
    """An object or repeated object group visible in the reference image."""

    name: str
    count: int = 1
    description: str = ""
    visual_attributes: list[str] = Field(default_factory=list)
    orientation: str = ""


class ReferenceRelation(BaseModel):
    """A spatial or support relationship visible in the reference image."""

    subject: str
    relation: str
    object: str
    description: str = ""


class ReferenceSceneSpec(BaseModel):
    """Structured, view-aware constraints extracted from a reference image."""

    summary: str
    scene_type: str = ""
    style_and_materials: list[str] = Field(default_factory=list)
    objects: list[ReferenceObject] = Field(default_factory=list)
    relations: list[ReferenceRelation] = Field(default_factory=list)
    composition_constraints: list[str] = Field(default_factory=list)
    camera_and_visibility: str = ""
    uncertain_observations: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ReferenceConditioningResult:
    """Prepared image, extracted constraints, and text prompt augmentation."""

    image_path: Path
    spec_path: Path
    spec: ReferenceSceneSpec
    prompt_suffix: str

    def to_agent_config(
        self, *, compare_during_critique: bool, vision_detail: str
    ) -> dict:
        """Create a serializable config payload for worker agent processes."""
        return {
            "enabled": True,
            "image_path": str(self.image_path),
            "spec_path": str(self.spec_path),
            "spec": self.spec.model_dump(mode="json"),
            "compare_during_critique": compare_during_critique,
            "vision_detail": vision_detail,
        }


def prepare_reference_conditioning(
    *,
    image_path: str | Path,
    output_dir: Path,
    model: str,
    max_dimension_px: int = 1024,
    vision_detail: str = "high",
    max_attempts: int = 3,
) -> ReferenceConditioningResult:
    """Analyze a reference image and persist its structured scene constraints."""
    source_path = Path(image_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Reference image not found: {source_path}")
    if max_dimension_px < 64:
        raise ValueError("reference_image.max_dimension_px must be at least 64")
    if vision_detail not in {"low", "high", "auto"}:
        raise ValueError("reference_image.vision_detail must be low, high, or auto")
    if max_attempts < 1:
        raise ValueError("reference_image.max_attempts must be at least 1")

    reference_dir = output_dir / "reference_conditioning"
    reference_dir.mkdir(parents=True, exist_ok=True)
    prepared_path = _prepare_image(
        source_path=source_path,
        output_path=reference_dir / "reference_image.png",
        max_dimension_px=max_dimension_px,
    )
    spec = _analyze_image(
        image_path=prepared_path,
        model=model,
        vision_detail=vision_detail,
        max_attempts=max_attempts,
    )
    spec_path = reference_dir / "reference_scene_spec.json"
    spec_path.write_text(
        json.dumps(spec.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    prompt_suffix = format_reference_prompt_suffix(spec)
    (reference_dir / "reference_prompt.txt").write_text(prompt_suffix, encoding="utf-8")
    shutil.copy2(source_path, reference_dir / f"original{source_path.suffix.lower()}")

    console_logger.info(
        "Reference image analyzed: %d objects, %d relations (%s)",
        len(spec.objects),
        len(spec.relations),
        spec_path,
    )
    return ReferenceConditioningResult(
        image_path=prepared_path,
        spec_path=spec_path,
        spec=spec,
        prompt_suffix=prompt_suffix,
    )


def format_reference_prompt_suffix(spec: ReferenceSceneSpec) -> str:
    """Convert structured reference constraints into a generation prompt suffix."""
    lines = [
        "# Reference-image constraints",
        "Use the following image-derived constraints as generation requirements. ",
        "Prioritize object identity, count, appearance, orientation, support, and ",
        "spatial relations. Preserve physical feasibility. The source is a single ",
        "view, so do not invent hidden geometry from uncertain observations.",
        f"Scene summary: {spec.summary}",
    ]
    if spec.scene_type:
        lines.append(f"Scene type: {spec.scene_type}")
    if spec.style_and_materials:
        lines.append("Style and materials: " + "; ".join(spec.style_and_materials))
    if spec.objects:
        lines.append("Required visible objects:")
        for item in spec.objects:
            details = [f"count={item.count}"]
            if item.description:
                details.append(item.description)
            if item.visual_attributes:
                details.append("attributes=" + ", ".join(item.visual_attributes))
            if item.orientation:
                details.append("orientation=" + item.orientation)
            lines.append(f"- {item.name}: " + "; ".join(details))
    if spec.relations:
        lines.append("Required relations:")
        for relation in spec.relations:
            relation_text = (
                f"- {relation.subject} {relation.relation} {relation.object}"
            )
            if relation.description:
                relation_text += f"; {relation.description}"
            lines.append(relation_text)
    if spec.composition_constraints:
        lines.append(
            "Composition constraints: " + "; ".join(spec.composition_constraints)
        )
    if spec.camera_and_visibility:
        lines.append("Reference view: " + spec.camera_and_visibility)
    if spec.uncertain_observations:
        lines.append(
            "Uncertain observations (do not treat as hard constraints): "
            + "; ".join(spec.uncertain_observations)
        )
    return "\n".join(lines)


def append_reference_constraints(prompt: str, prompt_suffix: str) -> str:
    """Append image-derived constraints to a user scene prompt exactly once."""
    if "# Reference-image constraints" in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{prompt_suffix}"


def image_path_to_data_url(image_path: Path) -> str:
    """Encode a prepared PNG reference image as a data URL."""
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_reference_critique_input(
    *, instruction: str, reference_config: dict
) -> str | list[dict]:
    """Attach the target image and constraints to a Critic runner instruction."""
    if not reference_config.get("enabled") or not reference_config.get(
        "compare_during_critique", True
    ):
        return instruction
    image_path = Path(str(reference_config.get("image_path", ""))).expanduser()
    if not image_path.is_file():
        raise FileNotFoundError(f"Critic reference image not found: {image_path}")
    spec = reference_config.get("spec", {})
    comparison_instruction = (
        f"{instruction}\n\n"
        "# Reference-image comparison\n"
        "The attached image is the target reference, not the current scene. Call "
        "observe_scene as required, then compare its current-scene renders against "
        "this target. Evaluate semantic content and explicit image-derived "
        "constraints rather than raw pixel similarity. Report mismatched object "
        "counts, appearance, orientation, support/contact, and spatial relations in "
        "the critique. Include reference adherence in Prompt Following while keeping "
        "physics and accessibility scores independent. The structured constraints "
        "describe the target only; never use them as evidence that the current scene "
        "already satisfies a constraint. Base current-scene claims only on visible "
        "observe_scene evidence. If an orientation or relation is occluded or "
        "ambiguous in every current render, explicitly mark it as unverifiable "
        "instead of guessing.\n"
        "Structured target constraints:\n"
        f"{json.dumps(spec, ensure_ascii=False, separators=(',', ':'))}"
    )
    detail = str(reference_config.get("vision_detail", "high"))
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": comparison_instruction},
                {
                    "type": "input_image",
                    "image_url": image_path_to_data_url(image_path),
                    "detail": detail,
                },
            ],
        }
    ]


def _prepare_image(
    *, source_path: Path, output_path: Path, max_dimension_px: int
) -> Path:
    with Image.open(source_path) as image:
        image.thumbnail((max_dimension_px, max_dimension_px))
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
    output_path.write_bytes(buffer.getvalue())
    return output_path


def _analyze_image(
    *, image_path: Path, model: str, vision_detail: str, max_attempts: int
) -> ReferenceSceneSpec:
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        max_retries=0,
    )
    schema = ReferenceSceneSpec.model_json_schema()
    instruction = (
        "Analyze this image as a target for generating a simulation-ready 3D scene. "
        "Extract only visible evidence. Identify objects and repeated counts, visual "
        "attributes, orientations, support/contact relations, containment, relative "
        "positions, scene style, composition, and camera visibility. Be especially "
        "precise about directional phrases such as upside down, lying flat, upright, "
        "inside, on top of, touching, or against. Record ambiguity under "
        "uncertain_observations. Return only one JSON object matching this schema: "
        + json.dumps(schema, separators=(",", ":"))
    )
    data_url = image_path_to_data_url(image_path)
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_url,
                                    "detail": vision_detail,
                                },
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("Reference image analysis returned no content")
            return ReferenceSceneSpec.model_validate_json(content)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(min(2**attempt, 10))
    raise RuntimeError(
        f"Reference image analysis failed after {max_attempts} attempts"
    ) from last_error
