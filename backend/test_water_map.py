"""Mapped water cannot be erased by a drawn boundary, site import or saved plan."""
import json

import pytest
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from backend.config import settings
from backend.execution import SafetyInterlockError
from backend.models import (Coordinate, EnvironmentMapData, GeoPolygon, OperatorRole,
                            PlanRequest, TaskItem, TaskType)
from backend.orchestrator import AgenticWorkflowOrchestrator
from backend.validators import LocationValidationError
from backend.water_map import (SNAPSHOT_PATH, mapped_water_features, merge_mapped_water,
                               water_map_coverage, water_snapshot_version)


def rectangle(south, west, north, east):
    return GeoPolygon(vertices=[Coordinate(lat=south, lon=west), Coordinate(lat=south, lon=east),
                                Coordinate(lat=north, lon=east), Coordinate(lat=north, lon=west)])


LAKE_POINT = Coordinate(lat=3.1407, lon=101.6855)
DRY_POINT = Coordinate(lat=3.14035, lon=101.6861)


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_default_campus_terrain_layer", True)
    engine = AgenticWorkflowOrchestrator(history_path=tmp_path / "history.json")
    engine.routing_engine._force_offline_for_test = True
    return engine


def test_real_water_snapshot_is_attributed_nonrectangular_and_valid():
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert data["metadata"]["license"] == "ODbL-1.0"
    assert data["metadata"]["source_url"].startswith("https://api.openstreetmap.org/")
    features = mapped_water_features()
    lake = next(feature for feature in features if "relation-8429206" in feature.feature_id)
    assert len(lake.polygon.vertices) > 100
    assert lake.polygon.to_shapely_local(LAKE_POINT).covers(Point(0, 0))
    assert all(feature.polygon.to_shapely_local(LAKE_POINT).is_valid for feature in features)
    assert len(features) == len(data["metadata"]["features"]) == 17
    assert water_map_coverage().to_shapely_local(DRY_POINT).covers(Point(0, 0))
    # Copy isolation: a caller must not mutate the shared safety dataset.
    features.clear()
    assert len(mapped_water_features()) == 17


def test_supplied_and_stale_maps_cannot_delete_known_water(instance):
    site = EnvironmentMapData(map_id="operator-site", features=[], require_verified_terrain=True,
        verified_traversable_polygons=[rectangle(3.1402, 101.685, 3.141, 101.6863)])
    merged = instance._resolve_environment_map(PlanRequest(environment_map=site))
    assert site.features == []
    assert merged.require_verified_terrain
    assert merged.verified_traversable_polygons == site.verified_traversable_polygons
    assert merged.water_map_version == water_snapshot_version()
    assert len(merged.features) == 17
    # A repeat uses its stored environment; resolving it refreshes the layer too.
    stale = EnvironmentMapData(map_id="kl-perdana-planetarium-gis", features=[], water_map_version="old")
    restored = instance._resolve_environment_map(PlanRequest(environment_map=stale))
    assert restored.water_map_version == water_snapshot_version()
    assert restored.hazard_coverage_boundary == water_map_coverage()
    assert len(merge_mapped_water(restored).features) == 17


@pytest.mark.parametrize("misplaced", ["start", "depot", "pickup"])
def test_water_pin_is_rejected_even_if_site_declares_lake_traversable(instance, misplaced):
    boundary = rectangle(3.1402, 101.685, 3.141, 101.6863)
    request = PlanRequest(start_location=DRY_POINT, disposal_location=DRY_POINT,
        operating_boundary=boundary, environment_map=EnvironmentMapData(map_id="mistaken-site",
            require_verified_terrain=True, verified_traversable_polygons=[boundary]))
    if misplaced == "start":
        request.start_location = LAKE_POINT
    elif misplaced == "depot":
        request.disposal_location = LAKE_POINT
    else:
        request.tasks = [TaskItem(task_id="water-bin", task_type=TaskType.RUBBISH_COLLECTION, location=LAKE_POINT)]
    with pytest.raises(LocationValidationError, match="inside water.*accessible dry land"):
        instance.plan_and_optimize(request)


def test_water_overlapping_boundary_keeps_dry_land_and_all_routes_clear(instance):
    boundary = rectangle(3.1403, 101.6852, 3.14085, 101.6862)
    route = instance.plan_and_optimize(PlanRequest(start_location=DRY_POINT, disposal_location=DRY_POINT,
        operating_boundary=boundary, enable_depot_cycles=True, use_rl=False))
    assert route.kpi_evaluation.passed, route.route_safety_issues
    assert any(segment.segment_type == "SWEEP_SWATH" for segment in route.segments)
    assert route.inaccessible_area_m2 > 500
    assert route.effective_eligible_area_m2 < route.requested_area_m2 - 500
    assert any("water_body_exclusion" in section.reason for section in route.planning_diagnostics.excluded_sections)
    origin = boundary.vertices[0]
    water = unary_union([feature.polygon.to_shapely_local(origin).buffer(feature.clearance_buffer_m - .02)
                         for feature in mapped_water_features()])
    machine = instance.machines[route.machine_id]
    for segment in route.segments:
        points = [point.to_local_meters(origin) for point in segment.waypoints]
        geometry = Point(points[0]) if len(points) == 1 else LineString(points)
        width = max(machine.clearance_width_m, machine.working_width_m) if segment.segment_type == "SWEEP_SWATH" else machine.clearance_width_m
        assert not geometry.buffer(width / 2).intersects(water), segment.segment_id


def test_preapproval_recheck_blocks_a_saved_pass_with_new_water_intersection(instance):
    route = instance.plan_and_optimize(PlanRequest(operating_boundary=rectangle(3.139, 101.6869, 3.13936, 101.6873), use_rl=False))
    assert route.kpi_evaluation.passed
    route.planning_environment_map = EnvironmentMapData(map_id="stale-empty-map")
    route.environment_features_applied = []
    route.segments[0].waypoints = [LAKE_POINT, Coordinate(lat=3.14071, lon=101.6855)]
    with pytest.raises(SafetyInterlockError, match="current water/access safety check"):
        instance.approve_route(route.route_id, "operator", OperatorRole.OPERATOR)
    assert not route.approved_by
    assert any("WATER_BODY" in issue for issue in route.route_safety_issues)
