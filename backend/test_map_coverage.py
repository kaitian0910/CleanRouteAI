"""Bounded hazard data must not imply hazard-free terrain beyond its coverage."""

import pytest
from shapely.geometry import LineString, Polygon, box

from backend.models import Coordinate, EnvironmentMapData, GeoPolygon, MachineProfile, TerrainFeature, TerrainFeatureType
from backend.planners import CoveragePlanner, TraversabilityGraph


ORIGIN = Coordinate(lat=0, lon=0)
MACHINE = MachineProfile(machine_id="coverage-test", name="Coverage test", clearance_width_m=1.0, working_width_m=1.6)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


def rectangle(x0, y0, x1, y1):
    return polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def graph(scope):
    return TraversabilityGraph(
        origin=ORIGIN, machine=MACHINE,
        free_navigation_space=box(-100, -100, 100, 100),
        impassable_hazard_union=Polygon(), prohibited_road_union=Polygon(),
        prohibited_surface_union=Polygon(),
        environment_map=EnvironmentMapData(hazard_coverage_boundary=scope),
    )


@pytest.mark.parametrize("role", ["start", "depot"])
@pytest.mark.parametrize("location", [(-1, 15), (0.2, 15)])
def test_portals_outside_map_coverage_or_overlapping_edge_are_rejected(role, location):
    navigation = graph(rectangle(0, 0, 40, 40))
    adjusted, reason, snapped = navigation.validate_portal_access(point(*location), role, allow_snap=True)
    assert adjusted is None and not snapped
    assert "outside mapped area; import site geometry" in reason


def test_safe_portal_inside_coverage_is_preserved_without_claiming_verified_terrain():
    navigation = graph(rectangle(0, 0, 40, 40))
    supplied = point(3, 3)
    assert navigation.validate_portal_access(supplied, "start", allow_snap=True) == (supplied, None, False)
    assert not navigation.require_verified_terrain
    assert navigation.free_navigation_space.area == pytest.approx(1600, abs=1)


def test_segment_and_stationary_footprints_cannot_leave_hazard_coverage():
    navigation = graph(rectangle(0, 0, 40, 40))
    assert navigation.is_segment_permitted((3, 3), (45, 3), require_within_boundary=False) == (
        False, "outside_mapped_hazard_coverage", None,
    )
    assert not navigation.is_segment_permitted((0.2, 15), (0.2, 15), require_within_boundary=False)[0]
    assert navigation.is_segment_permitted((3, 3), (35, 3), require_within_boundary=False)[0]


def test_route_around_unknown_gap_stays_within_coverage():
    scope = polygon([(0, 0), (40, 0), (40, 40), (25, 40), (25, 15), (15, 15), (15, 40), (0, 40)])
    navigation = graph(scope)
    assert not navigation.is_segment_permitted((5, 30), (35, 30), require_within_boundary=False)[0]
    path, _, _ = navigation.find_shortest_permitted_path((5, 30), (35, 30), require_within_boundary=False)
    assert path is not None
    footprint = LineString(path).buffer(MACHINE.clearance_width_m / 2)
    assert scope.to_shapely_local(ORIGIN).buffer(0.02).covers(footprint)


def test_coverage_excludes_outside_area_as_unverified_and_keeps_route_inside():
    scope = rectangle(0, 0, 30, 30)
    environment = EnvironmentMapData(hazard_coverage_boundary=scope)
    result = CoveragePlanner().plan_coverage(rectangle(0, 0, 50, 30), [], MACHINE,
        environment_map=environment, origin_coord=point(3, 3))
    assert result.unverified_area_m2 == pytest.approx(600, abs=1)
    assert result.effective_eligible_area_m2 == pytest.approx(900, abs=1)
    assert any("outside mapped area; import site geometry" in section.reason for section in result.excluded_sections)
    assert result.segments
    for segment in result.segments:
        footprint = LineString([p.to_local_meters(ORIGIN) for p in segment.waypoints]).buffer(
            max(MACHINE.clearance_width_m, MACHINE.working_width_m) / 2)
        assert scope.to_shapely_local(ORIGIN).buffer(0.02).covers(footprint)


def test_area_entirely_outside_coverage_does_not_generate_work():
    result = CoveragePlanner().plan_coverage(rectangle(60, 0, 80, 20), [], MACHINE,
        environment_map=EnvironmentMapData(hazard_coverage_boundary=rectangle(0, 0, 40, 40)),
        origin_coord=point(63, 3))
    assert result.unverified_area_m2 == pytest.approx(400, abs=1)
    assert result.effective_eligible_area_m2 == 0
    assert result.segments == []
    assert any("no_eligible_traversable_area" in issue for issue in result.route_safety_issues)


def test_explicit_map_without_coverage_boundary_preserves_its_existing_scope():
    environment = EnvironmentMapData(map_id="explicit-unbounded-test")
    result = CoveragePlanner().plan_coverage(rectangle(60, 0, 80, 20), [], MACHINE,
        environment_map=environment, origin_coord=point(63, 3))
    assert result.unverified_area_m2 == 0
    assert result.effective_eligible_area_m2 == pytest.approx(400, abs=1)
    assert result.segments


def test_body_wider_than_brush_remains_clear_of_water():
    machine = MACHINE.model_copy(update={"clearance_width_m": 4.0, "working_width_m": 1.0})
    water = rectangle(20, 5, 30, 25)
    environment = EnvironmentMapData(features=[TerrainFeature(name="Water", feature_type=TerrainFeatureType.WATER_BODY,
        polygon=water, clearance_buffer_m=0.4)])
    result = CoveragePlanner().plan_coverage(rectangle(0, 0, 50, 40), [], machine,
        environment_map=environment, origin_coord=point(4, 4))
    assert result.segments
    protected_water = water.to_shapely_local(ORIGIN).buffer(0.4 - 0.02)
    for segment in result.segments:
        footprint = LineString([p.to_local_meters(ORIGIN) for p in segment.waypoints]).buffer(machine.clearance_width_m / 2)
        assert not footprint.intersects(protected_water)
