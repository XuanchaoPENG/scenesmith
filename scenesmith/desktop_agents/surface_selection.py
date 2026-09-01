"""Selection policy for the single usable surface in desktop mode."""

import math

from scenesmith.agent_utils.room import SupportSurface


def select_primary_desktop_surface(
    surfaces: list[SupportSurface],
    *,
    max_tilt_degrees: float = 5.0,
    top_height_tolerance_m: float = 0.05,
) -> SupportSurface:
    """Return the largest near-horizontal surface at the top of a table.

    Tiny raised details can sit slightly above the real tabletop, so selection first
    finds the highest candidate band and then chooses the largest surface in it.
    """
    min_normal_z = math.cos(math.radians(max_tilt_degrees))
    candidates: list[SupportSurface] = []
    for surface in surfaces:
        normal_z = float(surface.transform.rotation().matrix()[2, 2])
        if normal_z >= min_normal_z:
            candidates.append(surface)

    if not candidates:
        raise ValueError(
            "No desktop support surface satisfies the configured tilt constraint "
            f"(max_tilt_degrees={max_tilt_degrees})"
        )

    highest_z = max(float(s.transform.translation()[2]) for s in candidates)
    top_band = [
        s
        for s in candidates
        if float(s.transform.translation()[2]) >= highest_z - top_height_tolerance_m
    ]
    return max(top_band, key=lambda surface: surface.area)
