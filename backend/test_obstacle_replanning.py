"""Exercise the same /routes/plan obstacle update sent by the map UI."""
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString

from backend import main
from backend.config import settings
from backend.models import Coordinate, ExclusionZone, GeoPolygon, PlanRequest
from backend.orchestrator import AgenticWorkflowOrchestrator


ORIGIN = Coordinate(lat=3.139, lon=101.6869)


def polygon(points):
    return GeoPolygon(vertices=[Coordinate.from_local_meters(x, y, ORIGIN) for x, y in points])


@pytest.fixture
def api(monkeypatch):
    workflow = AgenticWorkflowOrchestrator()
    workflow.routing_engine._force_offline_for_test = True
    monkeypatch.setattr(main, "orchestrator", workflow)
    with TestClient(main.app) as client:
        client.headers["Authorization"] = f"Bearer {settings.operator_token}"
        yield client, workflow


def test_map_obstacle_update_creates_new_route_around_exclusion_and_retains_progress(api):
    client, workflow = api
    req = PlanRequest(job_id="map-obstacle-test", use_rl=False,
                      operating_boundary=polygon([(0, 0), (45, 0), (45, 40), (0, 40)]),
                      start_location=Coordinate.from_local_meters(3, 3, ORIGIN),
                      disposal_location=Coordinate.from_local_meters(42, 3, ORIGIN))
    first = client.post("/api/routes/plan", json=req.model_dump(mode="json"))
    assert first.status_code == 200 and first.json()["kpi_evaluation"]["passed"]
    old = first.json()
    assert client.post(f"/api/routes/{old['route_id']}/approve").status_code == 200
    assert client.post(f"/api/routes/{old['route_id']}/execute", json={"stop_after_segments": 2}).status_code == 200
    frame = workflow.simulator.telemetry_store[old["route_id"]][-1]
    req.preserve_completed = True
    obstacle = ExclusionZone(zone_id="clicked-obstacle", name="Map obstacle", reason="Live Obstacle",
                             polygon=polygon([(19, 16), (27, 16), (27, 24), (19, 24)]))
    req.exclusion_zones = [obstacle]
    response = client.post("/api/routes/plan", json=req.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    revised = workflow.routes_by_id[response.json()["route_id"]]
    assert revised.route_version > old["route_version"]
    assert revised.kpi_evaluation.passed
    assert revised.origin == frame.position
    assert revised.mission_elapsed_min == frame.mission_elapsed_min
    assert any(segment.completed for segment in revised.segments)
    assert revised.exclusion_zones[0].zone_id == "clicked-obstacle"
    blocked = obstacle.polygon.to_shapely_local(ORIGIN)
    for segment in revised.segments:
        if not segment.completed and len(segment.waypoints) > 1:
            line = LineString([point.to_local_meters(ORIGIN) for point in segment.waypoints])
            assert not line.intersects(blocked)
    assert not revised.route_safety_issues


def test_obstacle_still_replans_an_oversized_blocked_mission_without_bypassing_limits(api):
    client, _ = api
    req = PlanRequest(job_id="map-blocked-obstacle", use_rl=False,
                      operating_boundary=polygon([(0, 0), (115, 0), (115, 155), (0, 155)]),
                      start_location=Coordinate.from_local_meters(3, 3, ORIGIN),
                      disposal_location=Coordinate.from_local_meters(110, 3, ORIGIN))
    first = client.post("/api/routes/plan", json=req.model_dump(mode="json")).json()
    assert not first["kpi_evaluation"]["passed"]
    req.preserve_completed = True
    req.exclusion_zones = [ExclusionZone(name="Added while blocked", polygon=polygon([(40, 40), (48, 40), (48, 48), (40, 48)]))]
    response = client.post("/api/routes/plan", json=req.model_dump(mode="json"))
    assert response.status_code == 200
    revised = response.json()
    assert revised["route_version"] > first["route_version"]
    assert revised["exclusion_zones"]
    assert not revised["kpi_evaluation"]["passed"]
    assert {"duration_exceeds_target_max", "energy_reserve_below_minimum"} & set(revised["kpi_evaluation"]["failed_constraints"])
    assert client.post(f"/api/routes/{revised['route_id']}/approve").status_code == 409
