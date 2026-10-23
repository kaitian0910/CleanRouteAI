"""Collection travel must honor the requested area as well as mapped terrain."""

import pytest
from shapely.geometry import LineString, Polygon, box

from backend.models import Coordinate, EnvironmentMapData, GeoPolygon, MachineProfile
from backend.planners import TraversabilityGraph, ValhallaRoutingService


ORIGIN = Coordinate(lat=0, lon=0)
MACHINE = MachineProfile(machine_id="concave-test", name="Concave boundary test")
CONCAVE_VERTICES = [
    (0, 0), (40, 0), (40, 40), (25, 40),
    (25, 12), (15, 12), (15, 40), (0, 40),
]


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def geo_polygon(vertices):
    return GeoPolygon(vertices=[point(x, y) for x, y in vertices])


def navigation(vertices, verified=False):
    boundary = Polygon(vertices)
    environment = None
    if verified:
        environment = EnvironmentMapData(
            require_verified_terrain=True,
            verified_traversable_polygons=[
                geo_polygon([(-20, -20), (60, -20), (60, 60), (-20, 60)]),
            ],
        )
    return TraversabilityGraph(
        origin=ORIGIN,
        machine=MACHINE,
        free_navigation_space=boundary,
        impassable_hazard_union=Polygon(),
        prohibited_road_union=Polygon(),
        prohibited_surface_union=Polygon(),
        environment_map=environment,
        requested_boundary_poly=boundary,
    )


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("vertical", [False, True])
def test_collection_matrix_detours_inside_concave_area_with_full_body_clearance(verified, vertical):
    vertices = [(y, x) for x, y in CONCAVE_VERTICES] if vertical else CONCAVE_VERTICES
    endpoints = [(30, 5), (30, 35)] if vertical else [(5, 30), (35, 30)]
    graph = navigation(vertices, verified=verified)
    locations = [point(x, y) for x, y in endpoints]
    matrix, paths = ValhallaRoutingService().compute_matrix_and_paths(
        locations, MACHINE.cruise_speed_mps, nav_graph=graph,
    )
    allowed = Polygon(vertices).buffer(0.03)
    # This chord crosses the missing middle of the U. The bin-to-bin route
    # must instead go around the concave corners, even on a broader site map.
    assert not allowed.covers(LineString(endpoints))
    for pair in [(0, 1), (1, 0)]:
        path = paths[pair]
        assert len(path) > 2
        footprint = LineString([p.to_local_meters(ORIGIN) for p in path]).buffer(
            MACHINE.clearance_width_m / 2,
        )
        assert allowed.covers(footprint)
        assert 30 < matrix.distance_matrix_m[pair[0]][pair[1]] < 1e9


def test_explicit_external_depot_access_uses_verified_footprint_outside_cleaning_area():
    graph = navigation(CONCAVE_VERTICES, verified=True)
    locations = [point(5, 5), point(-10, 5)]
    matrix, paths = ValhallaRoutingService().compute_matrix_and_paths(
        locations, MACHINE.cruise_speed_mps, nav_graph=graph,
    )
    for pair in [(0, 1), (1, 0)]:
        assert paths[pair]
        footprint = LineString([p.to_local_meters(ORIGIN) for p in paths[pair]]).buffer(
            MACHINE.clearance_width_m / 2,
        )
        assert box(-20, -20, 60, 60).covers(footprint)
        assert not Polygon(CONCAVE_VERTICES).covers(footprint)
        assert matrix.distance_matrix_m[pair[0]][pair[1]] < 1e9


@pytest.mark.parametrize("verified", [False, True])
def test_collection_matrix_does_not_shortcut_body_disconnected_boundary(verified):
    # The 0.8 m neck admits a centreline but cannot admit this 1.4 m machine.
    vertices = [
        (0, 0), (10, 0), (10, 4.6), (20, 4.6), (20, 0), (30, 0),
        (30, 10), (20, 10), (20, 5.4), (10, 5.4), (10, 10), (0, 10),
    ]
    graph = navigation(vertices, verified=verified)
    matrix, paths = ValhallaRoutingService().compute_matrix_and_paths(
        [point(5, 5), point(25, 5)], MACHINE.cruise_speed_mps, nav_graph=graph,
    )
    for pair in [(0, 1), (1, 0)]:
        assert paths[pair] == []
        assert matrix.distance_matrix_m[pair[0]][pair[1]] == 1e9
        assert matrix.duration_matrix_sec[pair[0]][pair[1]] == 1e9
