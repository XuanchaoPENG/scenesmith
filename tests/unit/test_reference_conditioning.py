"""Tests for top-level reference-image conditioning."""

import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from PIL import Image

import scenesmith.agent_utils.reference_conditioning as reference_conditioning

from scenesmith.agent_utils.reference_conditioning import (
    ReferenceConditioningResult,
    ReferenceObject,
    ReferenceRelation,
    ReferenceSceneSpec,
    append_reference_constraints,
    build_reference_critique_input,
    format_reference_prompt_suffix,
    prepare_reference_conditioning,
)


def _sample_spec() -> ReferenceSceneSpec:
    return ReferenceSceneSpec(
        summary="A tabletop stack with an inverted bowl.",
        scene_type="tabletop still life",
        objects=[
            ReferenceObject(
                name="white bowl",
                count=1,
                orientation="upside down",
                visual_attributes=["ceramic"],
            ),
            ReferenceObject(name="book", count=1, orientation="lying flat"),
        ],
        relations=[
            ReferenceRelation(
                subject="book",
                relation="on top of",
                object="white bowl",
            )
        ],
        composition_constraints=["centered on the table"],
    )


class TestReferenceConditioning(unittest.TestCase):
    def test_reference_prompt_preserves_orientation_and_relations(self) -> None:
        prompt = format_reference_prompt_suffix(_sample_spec())

        self.assertIn("orientation=upside down", prompt)
        self.assertIn("book on top of white bowl", prompt)
        self.assertIn("centered on the table", prompt)

    def test_append_reference_constraints_is_idempotent(self) -> None:
        suffix = format_reference_prompt_suffix(_sample_spec())
        prompt = append_reference_constraints("Create a simple room.", suffix)

        self.assertEqual(append_reference_constraints(prompt, suffix), prompt)
        self.assertEqual(prompt.count("# Reference-image constraints"), 1)

    def test_build_reference_critique_input_attaches_target_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "reference.png"
            Image.new("RGB", (8, 8), "white").save(image_path)
            spec = _sample_spec().model_dump(mode="json")

            result = build_reference_critique_input(
                instruction="Critique the current scene.",
                reference_config={
                    "enabled": True,
                    "compare_during_critique": True,
                    "image_path": str(image_path),
                    "vision_detail": "high",
                    "spec": spec,
                },
            )

        self.assertIsInstance(result, list)
        content = result[0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertIn("target reference, not the current scene", content[0]["text"])
        self.assertIn("never use them as evidence", content[0]["text"])
        self.assertIn("mark it as unverifiable", content[0]["text"])
        self.assertIn(
            json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
            content[0]["text"],
        )
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))

    def test_build_reference_critique_input_can_be_disabled(self) -> None:
        instruction = "Critique the current scene."

        self.assertEqual(
            build_reference_critique_input(
                instruction=instruction,
                reference_config={
                    "enabled": True,
                    "compare_during_critique": False,
                },
            ),
            instruction,
        )

    def test_prepare_reference_conditioning_persists_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "source.jpg"
            Image.new("RGB", (160, 80), "white").save(source_path)
            spec = _sample_spec()
            observed: dict = {}

            def fake_analyze_image(**kwargs):
                observed.update(kwargs)
                return spec

            with patch.object(
                reference_conditioning, "_analyze_image", fake_analyze_image
            ):
                result = prepare_reference_conditioning(
                    image_path=source_path,
                    output_dir=temp_path / "output",
                    model="test-model",
                    max_dimension_px=64,
                    vision_detail="high",
                    max_attempts=2,
                )

            self.assertIsInstance(result, ReferenceConditioningResult)
            self.assertTrue(result.image_path.is_file())
            self.assertTrue(result.spec_path.is_file())
            self.assertTrue(
                (result.image_path.parent / "reference_prompt.txt").is_file()
            )
            self.assertTrue((result.image_path.parent / "original.jpg").is_file())
            with Image.open(result.image_path) as prepared_image:
                self.assertEqual(prepared_image.size, (64, 32))
            self.assertEqual(observed["image_path"], result.image_path)
            self.assertEqual(observed["model"], "test-model")
            self.assertEqual(
                ReferenceSceneSpec.model_validate_json(
                    result.spec_path.read_text(encoding="utf-8")
                ),
                spec,
            )


if __name__ == "__main__":
    unittest.main()
