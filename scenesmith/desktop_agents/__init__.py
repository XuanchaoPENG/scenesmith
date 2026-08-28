"""Agents and helpers for focused desktop-scene generation."""

from .desktop_furniture_agent import DesktopFurnitureAgent, DesktopSceneSpec
from .desktop_manipuland_agent import DesktopManipulandAgent
from .surface_selection import select_primary_desktop_surface
from .validation import DesktopValidationResult, validate_desktop_scene

__all__ = [
    "DesktopFurnitureAgent",
    "DesktopManipulandAgent",
    "DesktopSceneSpec",
    "DesktopValidationResult",
    "select_primary_desktop_surface",
    "validate_desktop_scene",
]
