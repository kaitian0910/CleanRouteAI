"""Boundary portals permit local access while retaining independent hazard checks."""
import pytest

from .models import (Coordinate, ExclusionZone, GeoPolygon, MachineProfile, RouteCandidate,
                     RouteSegment, TaskType, TerrainFeature, TerrainFeatureType)
from .validators import TaskAndLocationValidator


def point(x, y):
    return Coordinate.from_local_meters(x, y, Coordinate(lat=3.14, lon=101.68))


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


def route_for(segment):
    return RouteCandidate(job_id="corner-access", route_version=1, machine_id="test",
        task_mode=TaskType.HYBRID, origin=point(0, 0), disposal_depot=point(0, 0),
        operating_boundary=polygon([(0, 0), (30, 0), (30, 30), (0, 30)]), segments=[segment])


def check(route):
    return TaskAndLocationValidator().validate_route_geometry(route, MachineProfile(machine_id="test", name="test"))


@pytest.mark.parametrize("kind,points", [
    ("TRANSIT", [(0, 0), (10, 10)]),
    ("RETURN_TO_BASE", [(10, 10), (0, 0)]),
])
def test_corner_portal_local_ingress_and_egress_are_valid(kind, points):
    segment = RouteSegment(segment_type=kind, waypoints=[point(x, y) for x, y in points],
        distance_m=15, estimated_duration_sec=15, target_task_id="bin")
    assert check(route_for(segment)) == []


def test_internal_collection_transit_still_needs_full_body_clearance():
    segment = RouteSegment(segment_type="TRANSIT", waypoints=[point(3, 0.2), point(15, 0.2)],
        distance_m=12, estimated_duration_sec=12, target_task_id="bin")
    assert any("clearance envelope leaves operating boundary" in issue for issue in check(route_for(segment)))


def test_portal_exception_does_not_extend_along_entire_boundary_leg():
    segment = RouteSegment(segment_type="TRANSIT", waypoints=[point(0, 0), point(25, 0.2)],
        distance_m=25, estimated_duration_sec=25, target_task_id="bin")
    assert any("clearance envelope leaves operating boundary" in issue for issue in check(route_for(segment)))


@pytest.mark.parametrize("kind", ["COLLECTION_STOP", "SWEEP_SWATH", "WAIT_FOR_WINDOW"])
def test_portal_is_not_a_work_or_wait_clearance_exemption(kind):
    segment = RouteSegment(segment_type=kind, waypoints=[point(0, 0)],
        distance_m=0, estimated_duration_sec=10, target_task_id="bin")
    assert any("clearance envelope leaves operating boundary" in issue for issue in check(route_for(segment)))


@pytest.mark.parametrize("hazard", ["exclusion", "water", "road"])
def test_portal_access_still_rejects_exclusions_water_and_public_road(hazard):
    segment = RouteSegment(segment_type="TRANSIT", waypoints=[point(0, 0), point(10, 10)],
        distance_m=15, estimated_duration_sec=15, target_task_id="bin")
    route = route_for(segment)
    obstruction = polygon([(3, 3), (6, 3), (6, 6), (3, 6)])
    if hazard == "exclusion":
        route.exclusion_zones = [ExclusionZone(name="blocked", polygon=obstruction)]
    else:
        route.environment_features_applied = [TerrainFeature(name="blocked", polygon=obstruction,
            feature_type=TerrainFeatureType.WATER_BODY if hazard == "water" else TerrainFeatureType.PUBLIC_ROAD)]
    issues = check(route)
    assert issues
    assert not any("leaves operating boundary" in issue for issue in issues)
