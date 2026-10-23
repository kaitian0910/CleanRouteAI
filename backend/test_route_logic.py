"""Physical coverage and efficient obstacle navigation regressions."""

import pytest
from shapely.geometry import LineString
from shapely.ops import unary_union

from backend.models import Coordinate, EnvironmentMapData, ExclusionZone, GeoPolygon, MachineProfile
from backend.planners import CoveragePlanner


ORIGIN = Coordinate(lat=0.0, lon=0.0)


def polygon(points):
    return GeoPolygon(vertices=[Coordinate.from_local_meters(x, y, ORIGIN) for x, y in points])


def plan(boundary, obstacles=(), start=(0, 0)):
    machine = MachineProfile(machine_id="physical-test", name="Test sweeper", working_width_m=2.0, clearance_width_m=1.0)
    return machine, CoveragePlanner().plan_coverage(
        boundary, list(obstacles), machine,
        origin_coord=Coordinate.from_local_meters(*start, ORIGIN),
        environment_map=EnvironmentMapData(map_id="test-empty-terrain"),
    )


def test_large_area_coverage_uses_actual_machine_width():
    boundary = polygon([(0, 0), (100, 0), (100, 60), (0, 60)])
    machine, result = plan(boundary)
    swaths = [s for s in result.segments if s.segment_type == "SWEEP_SWATH"]
    assert all(s.swath_width_m == machine.working_width_m for s in swaths)
    physical_coverage = unary_union([
        LineString([p.to_local_meters(ORIGIN) for p in s.waypoints]).buffer(machine.working_width_m / 2)
        for s in swaths
    ]).intersection(boundary.to_shapely_local(ORIGIN)).area
    # API latitude/longitude waypoints are rounded to seven decimal places.
    assert result.covered_area_m2 == pytest.approx(physical_coverage, abs=1.0)
    assert result.sweep_distance_m >= physical_coverage / machine.working_width_m - 100


def test_complete_each_side_of_obstacle_before_moving_to_next_block():
    boundary = polygon([(0, 0), (60, 0), (60, 40), (0, 40)])
    obstacle = ExclusionZone(name="building", polygon=polygon([(25, 8), (35, 8), (35, 32), (25, 32)]))
    _, result = plan(boundary, [obstacle])
    # Previously it detoured around the building on every row (1,233m of connectors).
    assert result.turn_distance_m < 200
    assert result.coverage_percent > 95
    assert not result.route_safety_issues
    blocked = obstacle.polygon.to_shapely_local(ORIGIN)
    for segment in result.segments:
        line = LineString([p.to_local_meters(ORIGIN) for p in segment.waypoints])
        assert not line.intersects(blocked)
    for previous, following in zip(result.segments, result.segments[1:]):
        a = previous.waypoints[-1].to_local_meters(ORIGIN)
        b = following.waypoints[0].to_local_meters(ORIGIN)
        assert LineString([a, b]).length < 0.01


def test_sweep_begins_at_nearby_corner_of_selected_area():
    boundary = polygon([(0, 0), (60, 0), (60, 40), (0, 40)])
    _, result = plan(boundary, start=(59, 39))
    first = result.segments[0].waypoints[0].to_local_meters(ORIGIN)
    assert LineString([(59, 39), first]).length < 3
