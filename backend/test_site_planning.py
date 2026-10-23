"""Explicit site imports and independent transit-footprint enforcement."""
import json
from pathlib import Path

import pytest

from backend.models import (
    AuthorizedCrossing, Coordinate, EnvironmentMapData, GeoPolygon, MachineProfile, OperatorRole,
    PlanRequest, RouteCandidate, RouteSegment, TaskType, TerrainFeature, TerrainFeatureType,
)
from backend.orchestrator import AgenticWorkflowOrchestrator
from backend.execution import SafetyInterlockError
from backend.site_planning import import_site_geojson
from backend.validators import TaskAndLocationValidator


def example():
    return json.loads((Path(__file__).parent / "examples" / "illustrative_site.geojson").read_text())


def test_import_preserves_explicit_scope_and_machine_parameters():
    imported = import_site_geojson(example(), {"machine_id": "site-machine", "name": "Configured sweeper", "working_width_m": 1.9})
    request = PlanRequest(**imported.plan_fields)
    assert request.environment_map.require_verified_terrain
    assert len(request.environment_map.verified_traversable_polygons) == 1
    assert len(request.exclusion_zones) == len(request.tasks) == 1
    assert request.machine_id == imported.machine_profile.machine_id == "site-machine"
    assert imported.machine_profile.working_width_m == 1.9
    assert imported.source == "OPERATOR_PROVIDED_GEOJSON"
    assert imported.warnings


@pytest.mark.parametrize("case,match", [
    ("missing_traversable", "at least one explicit traversable"),
    ("holes", "holes are unsupported"), ("multipolygon", "requires Polygon"),
    ("unknown_role", "properties.role"), ("nav_edges", "nav_edges are unsupported"),
    ("bad_coordinates", "Coordinates must be numeric"), ("duplicate_id", "Duplicate feature id"),
])
def test_import_rejects_ambiguous_or_unsupported_geometry(case, match):
    data = example()
    if case == "missing_traversable":
        data["features"] = [f for f in data["features"] if f["properties"]["role"] != "traversable"]
    elif case == "holes":
        data["features"][0]["geometry"]["coordinates"].append(data["features"][0]["geometry"]["coordinates"][0])
    elif case == "multipolygon":
        data["features"][0]["geometry"]["type"] = "MultiPolygon"
    elif case == "unknown_role":
        data["features"][0]["properties"]["role"] = "surveyed_magic_road"
    elif case == "nav_edges":
        data["nav_edges"] = [{"source_id": "a", "target_id": "b"}]
    elif case == "bad_coordinates":
        data["features"][2]["geometry"]["coordinates"] = [True, 3.139]
    elif case == "duplicate_id":
        data["features"][1]["id"] = data["features"][0]["id"]
    with pytest.raises(ValueError, match=match):
        import_site_geojson(data)


def test_import_rejects_unknown_machine_parameter():
    with pytest.raises(ValueError, match="supported MachineProfile"):
        import_site_geojson(example(), {"machine_id": "x", "name": "x", "unrecognized_battery": 100})


ORIGIN = Coordinate(lat=3.139, lon=101.6869)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


def test_transit_cannot_cross_unmapped_gap_even_without_boundary_constraint():
    boundary = polygon([(0, 0), (50, 0), (50, 20), (0, 20)])
    environment = EnvironmentMapData(map_id="disconnected-declared-footprints", require_verified_terrain=True,
        verified_traversable_polygons=[polygon([(0, 0), (10, 0), (10, 20), (0, 20)]),
                                       polygon([(40, 0), (50, 0), (50, 20), (40, 20)])])
    machine = MachineProfile(machine_id="x", name="x")
    request = PlanRequest(operating_boundary=boundary, start_location=point(3, 3), disposal_location=point(45, 3), environment_map=environment)
    instance = AgenticWorkflowOrchestrator()
    graph = instance._build_nav_graph_for_request(request, machine, environment)
    a, b = point(3, 3).to_local_meters(graph.origin), point(45, 3).to_local_meters(graph.origin)
    assert not graph.is_segment_permitted(a, b, require_within_boundary=False)[0]
    assert graph.find_shortest_permitted_path(a, b, require_within_boundary=False)[0] is None
    route = RouteCandidate(job_id="gap", route_version=1, machine_id="x", task_mode=TaskType.AREA_SWEEP,
        origin=point(3, 3), disposal_depot=point(45, 3), operating_boundary=boundary,
        planning_environment_map=environment,
        segments=[RouteSegment(segment_type="TRANSIT", waypoints=[point(3, 3), point(45, 3)], distance_m=42, estimated_duration_sec=42)])
    assert any("operator-declared traversable" in issue for issue in TaskAndLocationValidator().validate_route_geometry(route, machine))


def test_declared_access_can_reach_depot_outside_cleaning_boundary():
    environment = EnvironmentMapData(map_id="external-depot-access", require_verified_terrain=True,
        verified_traversable_polygons=[polygon([(0, 0), (50, 0), (50, 30), (0, 30)])])
    request = PlanRequest(operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
                          start_location=point(3, 3), disposal_location=point(45, 3), environment_map=environment)
    graph = AgenticWorkflowOrchestrator()._build_nav_graph_for_request(request, MachineProfile(machine_id="x", name="x"), environment)
    assert graph.find_shortest_permitted_path(point(3, 3).to_local_meters(graph.origin),
        point(45, 3).to_local_meters(graph.origin), require_within_boundary=False)[0]


def test_authorized_crossing_cannot_override_undeclared_access_footprint():
    environment = EnvironmentMapData(map_id="crossing-outside-footprint", require_verified_terrain=True,
        verified_traversable_polygons=[polygon([(0, 0), (10, 0), (10, 20), (0, 20)])],
        authorized_crossings=[AuthorizedCrossing(name="Unsupported gap crossing", allowed_machine_ids=["x"],
            start_point=point(8, 10), end_point=point(30, 10),
            corridor_polygon=polygon([(7, 8), (31, 8), (31, 12), (7, 12)]))])
    request = PlanRequest(start_location=point(3, 3), disposal_location=point(8, 3),
                          operating_boundary=polygon([(0, 0), (40, 0), (40, 20), (0, 20)]), environment_map=environment)
    graph = AgenticWorkflowOrchestrator()._build_nav_graph_for_request(request, MachineProfile(machine_id="x", name="x"), environment)
    assert not graph.permitted_crossings
    assert any("operator-declared traversable" in reason for reason in graph.prohibited_crossing_reasons)


def test_explicit_unverified_patch_blocks_transit_inside_declared_footprint():
    footprint = polygon([(0, 0), (50, 0), (50, 30), (0, 30)])
    environment = EnvironmentMapData(map_id="uncertain-patch", require_verified_terrain=True,
        verified_traversable_polygons=[footprint], features=[TerrainFeature(name="Unsurveyed strip",
            feature_type=TerrainFeatureType.UNVERIFIED_TERRAIN,
            polygon=polygon([(20, 0), (25, 0), (25, 30), (20, 30)]))])
    request = PlanRequest(operating_boundary=footprint, start_location=point(3, 3),
                          disposal_location=point(45, 3), environment_map=environment)
    graph = AgenticWorkflowOrchestrator()._build_nav_graph_for_request(request, MachineProfile(machine_id="x", name="x"), environment)
    assert graph.find_shortest_permitted_path(point(3, 3).to_local_meters(graph.origin),
        point(45, 3).to_local_meters(graph.origin), require_within_boundary=False)[0] is None


def test_imported_site_plans_and_simulates_with_its_scope_retained():
    imported = import_site_geojson(example())
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    request = PlanRequest(**imported.plan_fields, use_rl=False)
    route = instance.plan_and_optimize(request, actor="test")
    assert route.kpi_evaluation.passed, (route.kpi_evaluation.failed_constraints, route.route_safety_issues)
    assert route.planning_environment_map.map_id == request.environment_map.map_id
    assert route.planning_environment_map.require_verified_terrain
    assert route.planning_environment_map.verified_traversable_polygons == request.environment_map.verified_traversable_polygons
    assert all(feature in route.planning_environment_map.features for feature in request.environment_map.features)
    assert any(feature.feature_type == TerrainFeatureType.WATER_BODY for feature in route.planning_environment_map.features)
    instance.approve_route(route.route_id, actor="test", role=OperatorRole.OPERATOR)
    instance.simulator.execute_simulation(route, instance.machines[route.machine_id])
    assert all(segment.completed for segment in route.segments)
    assert all(task.completed for task in route.tasks)


def test_profile_revision_is_stamped_and_stale_geometry_cannot_be_approved():
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    machine = instance.machines["sweeper-alpha-01"]
    machine.planning_revision = 3
    route = instance.plan_and_optimize(PlanRequest(
        job_id="profile-revision", use_rl=False, start_location=point(3, 3), disposal_location=point(17, 3),
        operating_boundary=polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),
        environment_map=EnvironmentMapData(map_id="profile-test-clear")), actor="test")
    assert route.kpi_evaluation.passed
    assert route.machine_profile_revision == 3
    machine.planning_revision = 4
    with pytest.raises(SafetyInterlockError, match="Machine parameters changed"):
        instance.approve_route(route.route_id, actor="test", role=OperatorRole.OPERATOR)
