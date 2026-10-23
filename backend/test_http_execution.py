"""Exercise browser-style one-step HTTP requests, including depot and replan flows."""
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient

from .config import settings
from .models import EnvironmentMapData, ExclusionZone, PlanRequest
from .test_depot_mission import polygon, point, workflow


@pytest.fixture
def api(monkeypatch):
    from . import main
    instance = workflow()
    instance.machines["sweeper-alpha-01"].battery_capacity_kwh = 0.6
    monkeypatch.setattr(main, "orchestrator", instance)
    monkeypatch.setattr(settings.optimization, "max_iterations", 1)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "operator_token", "step-http-token")
    client = TestClient(main.app)
    return instance, client, {"Authorization": "Bearer step-http-token"}


def initial_plan(client, headers):
    request = PlanRequest(enable_depot_cycles=True, use_rl=False,
        operating_boundary=polygon([(0, 0), (45, 0), (45, 40), (0, 40)]),
        start_location=point(3, 3), disposal_location=point(42, 3),
        environment_map=EnvironmentMapData(map_id="http-clear-test"))
    response = client.post("/api/routes/plan", json=request.model_dump(mode="json"), headers=headers)
    assert response.status_code == 200, response.text
    route = response.json()
    assert route["kpi_evaluation"]["passed"], route["kpi_evaluation"]
    assert route["depot_visits"] > 0
    assert client.post(f"/api/routes/{route['route_id']}/approve", headers=headers).status_code == 200
    return request, route


def step(client, headers, route_id, endpoint, count):
    return client.post(f"/api/routes/{route_id}/{endpoint}", headers=headers, json={
        "stop_after_segments": 1, "idempotency_key": f"{route_id}-step-{count}",
        "expected_completed_segments": count,
    })


def test_http_step_execution_replans_around_obstacle_recharges_and_completes(api):
    instance, client, headers = api
    request, first = initial_plan(client, headers)
    route_id = first["route_id"]
    for count in range(2):
        response = step(client, headers, route_id, "execute" if count == 0 else "resume", count)
        assert response.status_code == 200, response.text
    old = instance.routes_by_id[route_id]
    completed = {segment.segment_id for segment in old.segments if segment.completed}
    position = instance.simulator.telemetry_store[route_id][-1].position
    request.preserve_completed = True
    request.exclusion_zones = [ExclusionZone(name="live-obstacle",
        polygon=polygon([(19, 16), (27, 16), (27, 24), (19, 24)]))]
    replanned = client.post("/api/routes/plan", json=request.model_dump(mode="json"), headers=headers)
    assert replanned.status_code == 200, replanned.text
    revised = replanned.json()
    assert revised["kpi_evaluation"]["passed"], revised["kpi_evaluation"]
    assert revised["route_version"] > first["route_version"]
    route_id = revised["route_id"]
    live = instance.routes_by_id[route_id]
    assert live.origin == position
    assert {s.segment_id for s in live.segments if s.completed} == completed
    assert client.post(f"/api/routes/{route_id}/approve", headers=headers).status_code == 200
    pending = sum(not s.completed for s in live.segments)
    for index in range(pending):
        count = len(completed) + index
        response = step(client, headers, route_id, "execute" if index == 0 else "resume", count)
        assert response.status_code == 200, response.text
        assert sum(segment.completed for segment in live.segments) == count + 1
    assert all(segment.completed for segment in live.segments)
    frames = instance.simulator.telemetry_store[route_id]
    assert frames[-1].position == live.disposal_depot
    assert frames[-1].mission_elapsed_min == pytest.approx(live.estimated_completion_minutes, abs=0.02)
    assert any(segment.segment_type == "DEPOT_RECHARGE" and segment.completed for segment in live.segments)
    assert instance.state_machine.get_state(live.job_id).value == "COMPLETED"


def test_repeated_execute_and_resume_requests_return_saved_snapshot_without_advancing(api):
    instance, client, headers = api
    _, planned = initial_plan(client, headers)
    route_id = planned["route_id"]
    first = step(client, headers, route_id, "execute", 0)
    duplicate = step(client, headers, route_id, "execute", 0)
    assert first.status_code == duplicate.status_code == 200
    assert first.json() == duplicate.json()
    assert sum(s.completed for s in instance.routes_by_id[route_id].segments) == 1
    second = step(client, headers, route_id, "resume", 1)
    duplicate = step(client, headers, route_id, "resume", 1)
    assert second.status_code == duplicate.status_code == 200
    assert second.json() == duplicate.json()
    assert sum(s.completed for s in instance.routes_by_id[route_id].segments) == 2
    assert step(client, headers, route_id, "resume", 2).status_code == 200
    old_snapshot = step(client, headers, route_id, "resume", 1)
    assert old_snapshot.json() == second.json()
    assert sum(s.completed for s in instance.routes_by_id[route_id].segments) == 3


def test_stale_expected_count_and_reused_key_with_different_parameters_do_not_move(api):
    instance, client, headers = api
    _, planned = initial_plan(client, headers)
    route_id = planned["route_id"]
    assert step(client, headers, route_id, "execute", 0).status_code == 200
    changed = client.post(f"/api/routes/{route_id}/execute", headers=headers,
        json={"stop_after_segments": 2, "idempotency_key": f"{route_id}-step-0", "expected_completed_segments": 0})
    assert changed.status_code == 409
    stale = client.post(f"/api/routes/{route_id}/resume", headers=headers,
        json={"stop_after_segments": 1, "idempotency_key": "different-request", "expected_completed_segments": 0})
    assert stale.status_code == 409
    assert sum(s.completed for s in instance.routes_by_id[route_id].segments) == 1
    assert instance.state_machine.get_state(planned["job_id"]).value == "PAUSED"


def test_concurrent_duplicate_resume_requests_advance_only_once(api):
    instance, client, headers = api
    _, planned = initial_plan(client, headers)
    route_id = planned["route_id"]
    assert step(client, headers, route_id, "execute", 0).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(step, client, headers, route_id, "resume", 1) for _ in range(2)]
        responses = [future.result() for future in futures]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert sum(s.completed for s in instance.routes_by_id[route_id].segments) == 2


def test_invalid_initial_resume_does_not_poison_ready_state(api):
    instance, client, headers = api
    _, planned = initial_plan(client, headers)
    route_id = planned["route_id"]
    refused = step(client, headers, route_id, "resume", 0)
    assert refused.status_code == 409
    assert instance.state_machine.get_state(planned["job_id"]).value == "READY"
    assert step(client, headers, route_id, "execute", 0).status_code == 200
