"""Local visibility searches retain full hazard checks without remote-node growth."""

from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from backend.models import AuthorizedCrossing, Coordinate, EnvironmentMapData, GeoPolygon, MachineProfile
from backend.planners import TraversabilityGraph


ORIGIN = Coordinate(lat=0, lon=0)
MACHINE = MachineProfile(machine_id="visibility-test", name="Visibility test", clearance_width_m=1.0)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(shape):
    return GeoPolygon(vertices=[point(x, y) for x, y in list(shape.exterior.coords)[:-1]])


def graph(scope, hazards=Polygon(), road=Polygon(), crossings=(), verified=True):
    environment = EnvironmentMapData(
        require_verified_terrain=verified,
        verified_traversable_polygons=[polygon(scope)] if verified else [],
        authorized_crossings=list(crossings),
    )
    return TraversabilityGraph(origin=ORIGIN, machine=MACHINE,
        free_navigation_space=scope.difference(hazards).difference(road),
        impassable_hazard_union=hazards, prohibited_road_union=road,
        prohibited_surface_union=Polygon(), environment_map=environment,
        requested_boundary_poly=scope)


def test_hundreds_of_remote_water_polygons_do_not_expand_local_visibility_pairs(monkeypatch):
    local_water = box(10, 5, 20, 25)
    remote_water = [box(1000 + i * 15, 100, 1008 + i * 15, 120) for i in range(200)]
    hazards = unary_union([local_water] + remote_water)
    navigation = graph(box(-10, -10, 5000, 1000), hazards)
    original = navigation.is_segment_permitted
    checks = 0

    def count_checks(*args, **kwargs):
        nonlocal checks
        checks += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(navigation, "is_segment_permitted", count_checks)
    path, _, _ = navigation.find_shortest_permitted_path((3, 15), (30, 15), require_within_boundary=False)
    assert path is not None
    assert checks < 2000, f"Remote water expanded a local search to {checks} pair checks"
    assert not LineString(path).buffer(MACHINE.clearance_width_m / 2).intersects(hazards.buffer(-0.02))
    # Remote polygons are excluded only from candidate-node generation; collision
    # checks still reject a path through every part of the original water layer.
    assert not original((1001, 110), (1007, 110), require_within_boundary=False)[0]


def test_nonverified_local_water_detour_remains_available():
    water = box(20, 10, 30, 30)
    navigation = graph(box(0, 0, 50, 40), water, verified=False)
    path, _, _ = navigation.find_shortest_permitted_path((3, 20), (45, 20), require_within_boundary=False)
    assert path is not None
    assert not LineString(path).buffer(MACHINE.clearance_width_m / 2).intersects(water.buffer(-0.02))


def test_expanding_search_retains_a_distant_authorized_crossing():
    scope = box(0, -100, 40, 100)
    road = box(18, -100, 22, 100)
    crossing = AuthorizedCrossing(crossing_id="distant-safe-crossing", name="Distant safe crossing",
        allowed_machine_ids=[MACHINE.machine_id], width_m=6,
        start_point=point(16, 80), end_point=point(24, 80),
        corridor_polygon=polygon(box(15, 77, 25, 83)))
    navigation = graph(scope, road=road, crossings=[crossing])
    path, _, used = navigation.find_shortest_permitted_path((5, 0), (35, 0), require_within_boundary=False)
    assert path is not None
    assert crossing.crossing_id in used
    assert max(y for _, y in path) > 70
    assert all(navigation.is_segment_permitted(a, b, require_within_boundary=False)[0]
               for a, b in zip(path, path[1:]))


def test_disconnected_verified_components_fail_without_remote_visibility_search(monkeypatch):
    scope = box(0, 0, 40, 40)
    navigation = graph(scope, hazards=box(18, -5, 22, 45))
    original = navigation.is_segment_permitted
    checks = 0

    def count_checks(*args, **kwargs):
        nonlocal checks
        checks += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(navigation, "is_segment_permitted", count_checks)
    assert navigation.find_shortest_permitted_path((5, 20), (35, 20), require_within_boundary=False)[0] is None
    assert checks <= 1


def test_visibility_cannot_start_from_a_water_footprint():
    water = box(10, 5, 20, 25)
    navigation = graph(box(0, 0, 40, 30), water)
    assert navigation.find_shortest_permitted_path((9.8, 15), (30, 15), require_within_boundary=False)[0] is None
