"""Import explicitly supplied site geometry; no map/survey trust is inferred."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from .config import CompatBaseModel
from .models import (
    Coordinate, EnvironmentMapData, ExclusionZone, GeoPolygon, MachineProfile,
    TaskItem, TaskType, TerrainFeature, TerrainFeatureType,
)


class SiteImportResult(CompatBaseModel):
    plan_fields: Dict[str, Any]
    machine_profile: Optional[MachineProfile] = None
    source: str = "OPERATOR_PROVIDED_GEOJSON"
    warnings: List[str] = Field(default_factory=list)


def import_site_geojson(geojson: Dict[str, Any], machine: Optional[Dict[str, Any]] = None) -> SiteImportResult:
    """Convert WGS84 GeoJSON roles into an explicit, constrained planning request.

    `traversable` denotes the operator's declared usable footprint; its presence
    is required, but importing it is not evidence of a field survey or permission.
    Holes, multipolygons, street-network edges and crossings are rejected rather
    than discarded. Represent holes as separate exclusion features.
    """
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        raise ValueError("Provide a GeoJSON FeatureCollection using WGS84 [longitude, latitude] coordinates.")
    if geojson.get("crs") is not None:
        raise ValueError("Convert the site to WGS84 GeoJSON and remove its legacy crs field before importing.")
    metadata = geojson.get("properties") or {}
    if not isinstance(metadata, dict):
        raise ValueError("FeatureCollection properties must be an object.")
    if geojson.get("nav_edges") or metadata.get("nav_edges"):
        raise ValueError("Street-network nav_edges are unsupported by the site importer; provide explicit traversable polygons instead.")
    features = geojson.get("features")
    if not isinstance(features, list) or not features or len(features) > 1000:
        raise ValueError("Provide between 1 and 1000 site features.")
    allowed = {"boundary", "exclusion", "traversable", "terrain", "start", "depot", "bin"}
    boundary = start = depot = None
    exclusions, traversable, terrain, tasks = [], [], [], []
    identifiers = set()

    def coordinate(raw: Any) -> Coordinate:
        if (not isinstance(raw, (list, tuple)) or len(raw) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw)):
            raise ValueError("Coordinates must be numeric WGS84 [longitude, latitude] pairs; altitude is unsupported.")
        return Coordinate(lon=raw[0], lat=raw[1])

    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"Feature {index + 1} must have type Feature.")
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            raise ValueError(f"Feature {index + 1} requires properties and geometry objects.")
        role = properties.get("role")
        if not isinstance(role, str) or role not in allowed:
            raise ValueError(f"Feature {index + 1} needs properties.role: {', '.join(sorted(allowed))}.")
        if properties.get("nav_edges"):
            raise ValueError("Street-network nav_edges are unsupported; provide explicit traversable polygons instead.")
        identifier = str(feature.get("id", properties.get("id", f"site-{index + 1}")))
        if identifier in identifiers:
            raise ValueError(f"Duplicate feature id '{identifier}'; give every feature a unique id.")
        identifiers.add(identifier)
        name = str(properties.get("name", identifier))
        required_type = "Point" if role in {"start", "depot", "bin"} else "Polygon"
        if geometry.get("type") != required_type:
            raise ValueError(f"Feature '{identifier}' ({role}) requires {required_type}; split MultiPolygon features into separate polygons.")
        if required_type == "Polygon":
            rings = geometry.get("coordinates")
            if not isinstance(rings, list) or len(rings) != 1:
                raise ValueError(f"Feature '{identifier}': polygon holes are unsupported; draw each hole as a separate exclusion feature.")
            ring = rings[0]
            if not isinstance(ring, list) or len(ring) < 4 or len(ring) > 2000 or ring[0] != ring[-1]:
                raise ValueError(f"Feature '{identifier}' needs a closed polygon ring with 4–2000 coordinate pairs.")
            value = GeoPolygon(vertices=[coordinate(raw) for raw in ring[:-1]])
        else:
            value = coordinate(geometry.get("coordinates"))
        if role == "boundary":
            if boundary is not None:
                raise ValueError("Provide exactly one boundary feature.")
            boundary = value
        elif role == "start":
            if start is not None:
                raise ValueError("Provide exactly one start feature.")
            start = value
        elif role == "depot":
            if depot is not None:
                raise ValueError("Provide exactly one depot feature.")
            depot = value
        elif role == "exclusion":
            exclusions.append(ExclusionZone(zone_id=identifier, name=name,
                                             reason=str(properties.get("reason", "Operator-provided exclusion")), polygon=value))
        elif role == "traversable":
            traversable.append(value)
        elif role == "terrain":
            try:
                feature_type = TerrainFeatureType(properties.get("feature_type"))
            except (ValueError, TypeError):
                raise ValueError(f"Terrain '{identifier}' needs a supported feature_type: {', '.join(t.value for t in TerrainFeatureType)}.") from None
            terrain.append(TerrainFeature(feature_id=identifier, name=name, feature_type=feature_type,
                polygon=value, **{key: properties[key] for key in (
                    "clearance_buffer_m", "max_slope_deg", "min_clearance_width_m", "allowed_machine_ids") if key in properties}))
        elif role == "bin":
            tasks.append(TaskItem(task_id=identifier, task_type=TaskType.RUBBISH_COLLECTION, location=value,
                **{key: properties[key] for key in (
                    "priority", "service_duration_sec", "time_window_start_min", "time_window_end_min", "rubbish_spec") if key in properties}))
    if boundary is None or start is None or depot is None or not traversable:
        raise ValueError("Site import requires exactly one boundary, one start, one depot, and at least one explicit traversable polygon.")
    environment = EnvironmentMapData(map_id=str(geojson.get("name", "operator-provided-site")),
                                     features=terrain, verified_traversable_polygons=traversable,
                                     require_verified_terrain=True)
    if machine is not None:
        fields = getattr(MachineProfile, "model_fields", None) or MachineProfile.__fields__
        if not isinstance(machine, dict) or set(machine) - set(fields):
            raise ValueError("Machine profile must be an object containing supported MachineProfile parameters.")
    profile = MachineProfile.model_validate(machine) if machine is not None else None
    fields = {
        "operating_boundary": boundary.model_dump(mode="json"),
        "start_location": start.model_dump(mode="json"), "disposal_location": depot.model_dump(mode="json"),
        "exclusion_zones": [zone.model_dump(mode="json") for zone in exclusions],
        "environment_map": environment.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "enable_depot_cycles": True,
    }
    if profile:
        fields["machine_id"] = profile.machine_id
    return SiteImportResult(plan_fields=fields, machine_profile=profile, warnings=[
        "Geometry and access permissions are operator-provided. Import does not verify a survey, road permission, surface condition, or machine calibration.",
        "Planning and execution estimates use the configured machine speeds, energy rates, outing limits, and depot service duration; execution remains simulation.",
    ])
