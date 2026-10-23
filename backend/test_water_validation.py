"""Independent water checks guard dry centrelines and accidental map placements."""

import pytest

from backend.config import settings
from backend.models import (
    AuthorizedCrossing,
    Coordinate,
    EnvironmentMapData,
    GeoPolygon,
    MachineProfile,
    PlanRequest,
    RouteCandidate,
    RouteSegment,
    TaskItem,
    TaskType,
    TerrainFeature,
    TerrainFeatureType,
)
from backend.validators import (
    LocationValidationError,
    ParsedCommandIntent,
    TaskAndLocationValidator,
)
from backend.water_map import DEFAULT_MAP_ID, mapped_water_features, water_map_coverage


ORIGIN = Coordinate(lat=0.0, lon=0.0)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


def water_environment(clearance=0.0):
    return EnvironmentMapData(map_id="water-validation-test", features=[
        TerrainFeature(feature_id="test-lake", name="Test lake",
            feature_type=TerrainFeatureType.WATER_BODY,
            polygon=polygon([(10, 0), (20, 0), (20, 20), (10, 20)]),
            clearance_buffer_m=clearance),
    ])


def candidate(segment_type, x, environment, stationary=False, *, completed=False):
    return RouteCandidate(job_id="water-validation", route_version=1,
        machine_id="test-sweeper", task_mode=TaskType.AREA_SWEEP,
        origin=point(3, 3), disposal_depot=point(3, 17),
        planning_environment_map=environment,
        segments=[RouteSegment(segment_id="test-segment", segment_type=segment_type,
            waypoints=[point(x, 5)] if stationary else [point(x, 5), point(x, 15)],
            distance_m=0 if stationary else 10, estimated_duration_sec=10,
            completed=completed)])


MACHINE = MachineProfile(machine_id="test-sweeper", name="Test sweeper",
                         clearance_width_m=1.0, working_width_m=4.0)


@pytest.mark.parametrize("segment_type,stationary", [
    ("TRANSIT", False), ("RETURN_TO_BASE", False), ("TURN_CONNECTOR", False),
    ("COLLECTION_STOP", True), ("WAIT_FOR_WINDOW", True),
    ("DEPOT_RECHARGE", True), ("DISPOSAL_STOP", True),
])
def test_water_collision_checks_machine_body_for_travel_and_stationary_stops(segment_type, stationary):
    # The centreline lies on dry land, but half the machine extends into water.
    route = candidate(segment_type, 9.7, water_environment(), stationary)
    issues = TaskAndLocationValidator().validate_route_geometry(route, MACHINE)
    assert any("clearance envelope intersects impassable WATER_BODY 'Test lake'" in issue
               for issue in issues)


def test_sweep_checks_wider_brush_even_when_body_clears_water():
    route = candidate("SWEEP_SWATH", 8.7, water_environment())
    assert TaskAndLocationValidator().validate_route_geometry(route, MACHINE)
    # The same centreline is safe with the brush inactive during transit.
    route.segments[0].segment_type = "TRANSIT"
    assert not TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


def test_sweep_checks_body_even_when_brush_is_narrower_than_body():
    machine = MACHINE.model_copy(update={"clearance_width_m": 4.0, "working_width_m": 1.0})
    route = candidate("SWEEP_SWATH", 8.7, water_environment())
    assert TaskAndLocationValidator().validate_route_geometry(route, machine)


def test_water_shoreline_clearance_applies_beyond_physical_water_polygon():
    route = candidate("RETURN_TO_BASE", 9.0, water_environment(clearance=1.0))
    assert TaskAndLocationValidator().validate_route_geometry(route, MACHINE)
    route.segments[0].waypoints = [point(8, 5), point(8, 15)]
    assert not TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


def test_candidate_applied_features_still_guard_water_without_saved_environment():
    environment = water_environment()
    route = candidate("TRANSIT", 9.7, environment)
    route.environment_features_applied = environment.features
    route.planning_environment_map = None
    assert TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


def test_candidate_feature_snapshot_does_not_invent_builtin_map_coverage():
    environment = water_environment()
    route = candidate("TRANSIT", 5, environment)
    route.environment_features_applied = environment.features
    route.planning_environment_map = None
    assert not TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


def test_authorized_road_crossing_does_not_override_water_exclusion():
    environment = water_environment()
    environment.authorized_crossings = [AuthorizedCrossing(name="Road crossing",
        start_point=point(5, 10), end_point=point(25, 10),
        allowed_machine_ids=[MACHINE.machine_id], width_m=5.0,
        corridor_polygon=polygon([(4, 7), (26, 7), (26, 13), (4, 13)]))]
    route = candidate("TRANSIT", 15, environment)
    route.segments[0].crossing_id = environment.authorized_crossings[0].crossing_id
    assert any("WATER_BODY" in issue
        for issue in TaskAndLocationValidator().validate_route_geometry(route, MACHINE))


def test_completed_geometry_remains_historical_rather_than_being_reexecuted():
    route = candidate("TRANSIT", 15, water_environment(), completed=True)
    assert not TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


@pytest.mark.parametrize("placement,label", [
    ("start", "Start point"), ("depot", "Disposal depot point"), ("task", "Task 'bin-in-water'"),
])
def test_request_rejects_start_depot_or_pending_task_inside_resolved_water(placement, label):
    request = PlanRequest(
        operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
        start_location=point(3, 3), disposal_location=point(3, 20))
    if placement == "start":
        request.start_location = point(15, 10)
    elif placement == "depot":
        request.disposal_location = point(15, 10)
    else:
        request.tasks = [TaskItem(task_id="bin-in-water", task_type=TaskType.RUBBISH_COLLECTION,
                                  location=point(15, 10))]
    # Resolved mandatory map data is checked even if the browser supplied no map.
    with pytest.raises(LocationValidationError, match=f"{label} is inside water 'Test lake'"):
        TaskAndLocationValidator().validate_plan_request(request, ParsedCommandIntent(), water_environment())


def test_request_rejects_shoreline_margin_and_accepts_dry_placement():
    request = PlanRequest(
        operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
        start_location=point(3, 3), disposal_location=point(9.5, 10),
        environment_map=water_environment(clearance=1.0))
    with pytest.raises(LocationValidationError, match="shoreline safety margin"):
        TaskAndLocationValidator().validate_plan_request(request, ParsedCommandIntent())
    request.disposal_location = point(8, 10)
    assert TaskAndLocationValidator().validate_plan_request(request, ParsedCommandIntent()) == TaskType.AREA_SWEEP


def test_completed_task_location_is_not_reintroduced_as_pending_water_work():
    request = PlanRequest(
        operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
        start_location=point(3, 3), disposal_location=point(3, 20),
        environment_map=water_environment(), tasks=[
            TaskItem(task_id="historical-bin", task_type=TaskType.RUBBISH_COLLECTION,
                     location=point(15, 10), completed=True)])
    assert TaskAndLocationValidator().validate_plan_request(request, ParsedCommandIntent()) == TaskType.HYBRID


@pytest.mark.parametrize("placement,label", [
    ("start", "Start point"), ("depot", "Disposal depot point"), ("task", "Task 'outside-map'"),
])
def test_request_rejects_locations_outside_available_hazard_coverage(placement, label):
    environment = EnvironmentMapData(map_id="bounded-site",
        hazard_coverage_boundary=polygon([(0, 0), (20, 0), (20, 20), (0, 20)]))
    request = PlanRequest(operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
                          start_location=point(3, 3), disposal_location=point(3, 10),
                          environment_map=environment)
    if placement == "start":
        request.start_location = point(25, 10)
    elif placement == "depot":
        request.disposal_location = point(25, 10)
    else:
        request.tasks = [TaskItem(task_id="outside-map", task_type=TaskType.RUBBISH_COLLECTION,
                                  location=point(25, 10))]
    with pytest.raises(LocationValidationError, match=f"{label} is outside available hazard map coverage"):
        TaskAndLocationValidator().validate_plan_request(request, ParsedCommandIntent())


def test_partial_drawn_boundary_can_continue_with_valid_locations_for_planner_clipping():
    request = PlanRequest(operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
        start_location=point(3, 3), disposal_location=point(3, 10),
        environment_map=EnvironmentMapData(map_id="bounded-site",
            hazard_coverage_boundary=polygon([(0, 0), (20, 0), (20, 20), (0, 20)])))
    assert TaskAndLocationValidator().validate_plan_request(request, ParsedCommandIntent()) == TaskType.AREA_SWEEP


@pytest.mark.parametrize("segment_type,x,stationary", [
    ("TRANSIT", 19.7, False), ("RETURN_TO_BASE", 19.7, False),
    ("DEPOT_RECHARGE", 19.7, True), ("SWEEP_SWATH", 18.7, False),
])
def test_full_footprint_must_stay_inside_hazard_map_coverage(segment_type, x, stationary):
    environment = EnvironmentMapData(map_id="bounded-site",
        hazard_coverage_boundary=polygon([(0, 0), (20, 0), (20, 20), (0, 20)]))
    route = candidate(segment_type, x, environment, stationary)
    assert any("clearance envelope leaves available hazard map coverage" in issue
        for issue in TaskAndLocationValidator().validate_route_geometry(route, MACHINE))
    route.segments[0].waypoints = [point(5, 5), point(5, 15)]
    assert not TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


@pytest.mark.parametrize("map_source", ["empty-saved", "missing", "legacy-saved", "explicit-empty"])
def test_current_water_map_cannot_be_erased_by_missing_or_stale_candidate_data(map_source):
    water_feature = mapped_water_features()[0]
    origin = water_feature.polygon.vertices[0]
    location = water_feature.polygon.to_shapely_local(origin).representative_point()
    in_water = Coordinate.from_local_meters(location.x, location.y, origin)
    route = RouteCandidate(job_id="stale-water-map", route_version=1,
        machine_id=MACHINE.machine_id, task_mode=TaskType.AREA_SWEEP,
        origin=in_water, disposal_depot=in_water,
        planning_environment_map=None if map_source == "missing" else EnvironmentMapData(map_id=DEFAULT_MAP_ID),
        segments=[RouteSegment(segment_type="DEPOT_RECHARGE", waypoints=[in_water],
                               distance_m=0, estimated_duration_sec=60)])
    if map_source == "legacy-saved":
        route.planning_environment_map.water_map_version = "obsolete-snapshot"
        route.planning_environment_map.hazard_coverage_boundary = polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    provided = EnvironmentMapData(map_id="operator-map") if map_source == "explicit-empty" else None
    issues = TaskAndLocationValidator().validate_route_geometry(route, MACHINE, environment_map=provided)
    assert any("WATER_BODY" in issue for issue in issues)
    # A stale built-in coverage envelope is refreshed from the current snapshot.
    if map_source == "legacy-saved":
        assert not any("hazard map coverage" in issue for issue in issues)


def test_independent_map_merge_respects_explicit_disabled_setting(monkeypatch):
    monkeypatch.setattr(settings, "enable_default_campus_terrain_layer", False)
    feature = mapped_water_features()[0]
    origin = feature.polygon.vertices[0]
    location = feature.polygon.to_shapely_local(origin).representative_point()
    in_water = Coordinate.from_local_meters(location.x, location.y, origin)
    route = RouteCandidate(job_id="water-map-disabled", route_version=1,
        machine_id=MACHINE.machine_id, task_mode=TaskType.AREA_SWEEP,
        origin=in_water, disposal_depot=in_water,
        planning_environment_map=EnvironmentMapData(map_id="explicit-test-map"),
        segments=[RouteSegment(segment_type="DEPOT_RECHARGE", waypoints=[in_water],
                               distance_m=0, estimated_duration_sec=60)])
    assert not TaskAndLocationValidator().validate_route_geometry(route, MACHINE)


def test_legacy_builtin_map_refreshes_coverage_instead_of_trusting_empty_saved_map():
    coverage = water_map_coverage()
    outside = Coordinate(lat=coverage.vertices[0].lat - 0.01, lon=coverage.vertices[0].lon)
    route = RouteCandidate(job_id="legacy-map-outside", route_version=1,
        machine_id=MACHINE.machine_id, task_mode=TaskType.AREA_SWEEP,
        origin=outside, disposal_depot=outside,
        planning_environment_map=EnvironmentMapData(map_id=DEFAULT_MAP_ID),
        segments=[RouteSegment(segment_type="TRANSIT", waypoints=[outside],
                               distance_m=0, estimated_duration_sec=1)])
    assert any("hazard map coverage" in issue
        for issue in TaskAndLocationValidator().validate_route_geometry(route, MACHINE))
