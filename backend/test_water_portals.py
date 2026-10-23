"""A drawn cleaning boundary cannot relocate a start/depot out of water."""

import pytest
from shapely.geometry import LineString, Polygon, box

from backend.models import AuthorizedCrossing, Coordinate, EnvironmentMapData, GeoPolygon, MachineProfile
from backend.planners import TraversabilityGraph


ORIGIN = Coordinate(lat=0, lon=0)
MACHINE = MachineProfile(machine_id="portal-test", name="Portal test", clearance_width_m=1.0)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def graph(*, free_space=None, water=None, road=None, environment=None):
    return TraversabilityGraph(
        origin=ORIGIN,
        machine=MACHINE,
        free_navigation_space=box(0, 0, 100, 30) if free_space is None else free_space,
        impassable_hazard_union=Polygon() if water is None else water,
        prohibited_road_union=Polygon() if road is None else road,
        prohibited_surface_union=Polygon(),
        environment_map=environment,
        requested_boundary_poly=box(-10, -10, 110, 40),
    )


@pytest.mark.parametrize("role", ["start", "depot"])
@pytest.mark.parametrize("allow_snap", [False, True])
def test_water_portal_is_rejected_even_with_safe_land_inside_drawn_boundary(role, allow_snap):
    water = box(0, 0, 30, 30).buffer(1.0)
    navigation = graph(free_space=box(0, 0, 100, 30).difference(water), water=water)
    adjusted, reason, snapped = navigation.validate_portal_access(point(25, 15), role, allow_snap=allow_snap)

    assert adjusted is None
    assert f"{role}_access_unreachable" in reason
    assert "impassable hazard or its clearance buffer" in reason
    assert not snapped


def test_portal_footprint_must_clear_the_water_margin():
    water = box(0, 0, 30, 30).buffer(1.0)
    navigation = graph(free_space=box(0, 0, 100, 30).difference(water), water=water)
    # Centre is on land, but half of the 1 m machine extends into the margin.
    adjusted, reason, snapped = navigation.validate_portal_access(point(31.25, 15), "start", allow_snap=True)
    assert adjusted is None and reason and not snapped


def test_safe_land_portal_is_preserved():
    water = box(0, 0, 30, 30).buffer(1.0)
    navigation = graph(free_space=box(0, 0, 100, 30).difference(water), water=water)
    supplied = point(35, 15)
    assert navigation.validate_portal_access(supplied, "depot", allow_snap=True) == (supplied, None, False)


def test_short_adjustment_cannot_jump_over_water_between_portal_and_land():
    water = box(7, 0, 12, 30)
    navigation = graph(free_space=box(20, 0, 50, 30), water=water)
    adjusted, reason, snapped = navigation.validate_portal_access(point(4, 15), "start", allow_snap=True)
    assert adjusted is None and reason and not snapped


def test_drawn_boundary_does_not_expand_portal_snap_limit_to_250_metres():
    navigation = graph(free_space=box(60, 0, 100, 30), road=box(0, 0, 30, 30))
    adjusted, reason, snapped = navigation.validate_portal_access(point(10, 15), "depot", allow_snap=True)
    assert adjusted is None and reason and not snapped


def test_small_adjustment_retains_authorized_safe_access_connection():
    crossing = AuthorizedCrossing(
        crossing_id="safe-access", name="Safe access", allowed_machine_ids=[MACHINE.machine_id],
        start_point=point(-2, 10), end_point=point(3, 10), width_m=4,
        corridor_polygon=GeoPolygon(vertices=[point(-2, 8), point(3, 8), point(3, 12), point(-2, 12)]),
    )
    navigation = graph(free_space=box(2, 0, 30, 30), road=box(-1, 0, 1, 20),
                       environment=EnvironmentMapData(authorized_crossings=[crossing]))
    supplied = point(0, 10)
    adjusted, reason, snapped = navigation.validate_portal_access(supplied, "start", allow_snap=True)
    assert adjusted is not None and reason is None and snapped
    start_xy, end_xy = supplied.to_local_meters(ORIGIN), adjusted.to_local_meters(ORIGIN)
    assert LineString([start_xy, end_xy]).length < 18
    assert navigation.is_segment_permitted(start_xy, end_xy, require_within_boundary=False)[0]
