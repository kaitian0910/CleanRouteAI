"""Versioned local water exclusions shared by the browser and planner."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

from .models import Coordinate, EnvironmentMapData, GeoPolygon, TerrainFeature


DEFAULT_MAP_ID = "kl-perdana-planetarium-gis"
LEGACY_WATER_IDS = {"water-sungai-bras-bras-channel", "water-tasik-perdana-lake"}
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "shared" / "perdana-water.json"


@lru_cache(maxsize=1)
def _snapshot():
    # Local, reviewed JSON only. Missing/malformed data fails closed rather than
    # silently replacing a safety map with an empty list.
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    features = tuple(TerrainFeature.model_validate(feature) for feature in payload["features"])
    if not features:
        raise ValueError("The bundled water safety map is empty.")
    return payload["metadata"], features


def mapped_water_features():
    return [feature.model_copy(deep=True) for feature in _snapshot()[1]]


def water_snapshot_version():
    return _snapshot()[0]["snapshot_id"]


def water_map_coverage():
    west, south, east, north = _snapshot()[0]["coverage_bbox"]
    return GeoPolygon(vertices=[Coordinate(lat=south, lon=west), Coordinate(lat=south, lon=east),
                                Coordinate(lat=north, lon=east), Coordinate(lat=north, lon=west)])


def merge_mapped_water(environment: EnvironmentMapData) -> EnvironmentMapData:
    """An imported or saved traversable map cannot erase known local water.

    Positive site footprints and other operator features remain unchanged. For
    the built-in reference map, refresh its coverage and replace legacy water.
    """
    features = [feature.model_copy(deep=True) for feature in environment.features
                if feature.feature_id not in LEGACY_WATER_IDS and not feature.feature_id.startswith("water-osm-")]
    update = {"features": features + mapped_water_features(), "water_map_version": water_snapshot_version()}
    if environment.map_id == DEFAULT_MAP_ID:
        update["hazard_coverage_boundary"] = water_map_coverage()
    return environment.model_copy(deep=True, update=update)
