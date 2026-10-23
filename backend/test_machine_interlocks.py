"""A machine cannot be restarted through a different job after an emergency stop."""
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient

from .config import settings
from .execution import ExecutionStateMachine, SafetyInterlockError
from .models import (
    AuditAndEventManager, EnvironmentMapData, ExecutionState,
    OperatorRole, PlanRequest,
)
from .test_depot_mission import point, polygon, workflow


@pytest.fixture
def api(monkeypatch):
    from . import main
    instance = workflow()
    monkeypatch.setattr(main, "orchestrator", instance)
    monkeypatch.setattr(settings.optimization, "max_iterations", 1)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "operator_token", "interlock-test-token")
    return instance, TestClient(main.app), {"Authorization": "Bearer interlock-test-token"}


def plan(api, job_id, machine_id="sweeper-alpha-01"):
    _, client, headers = api
    request = PlanRequest(job_id=job_id, machine_id=machine_id,
        use_rl=False, enable_depot_cycles=True,
        operating_boundary=polygon([(0, 0), (15, 0), (15, 15), (0, 15)]),
        start_location=point(2, 2), disposal_location=point(12, 2),
        environment_map=EnvironmentMapData(map_id="interlock-clear-test"))
    response = client.post("/api/routes/plan", headers=headers, json=request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert response.json()["kpi_evaluation"]["passed"], response.text
    return response.json()


def approve(api, route):
    _, client, headers = api
    return client.post(f"/api/routes/{route['route_id']}/approve", headers=headers)


def test_new_job_cannot_bypass_same_machine_emergency_stop(api):
    instance, client, headers = api
    first = plan(api, "stopped-job")
    assert approve(api, first).status_code == 200
    response = client.post("/api/events", headers=headers, json={
        "job_id": first["job_id"], "route_id": first["route_id"],
        "event_type": "EMERGENCY_STOP",
    })
    assert response.status_code == 200, response.text
    # Generating a proposal is harmless; approval and execution remain blocked.
    second = plan(api, "fresh-job-after-stop")
    rejected = approve(api, second)
    assert rejected.status_code == 409, rejected.text
    assert "emergency stop" in rejected.json()["detail"].lower()
    assert instance.routes_by_id[second["route_id"]].status.value == "PROPOSED"
    response = client.post(f"/api/routes/{second['route_id']}/execute",
        headers=headers, json={"stop_after_segments": 1})
    assert response.status_code == 409, response.text
    assert not any(segment.completed for segment in instance.routes_by_id[second["route_id"]].segments)
    assert instance.state_machine.get_state(first["job_id"]) == ExecutionState.EMERGENCY_STOP
    # The existing public reset endpoint still cannot invent hardware clearance.
    assert client.post("/api/safety/reset-estop", headers=headers).status_code == 501
    assert approve(api, second).status_code == 409


def test_an_approved_or_paused_job_reserves_its_machine_until_completion(api):
    instance, client, headers = api
    first = plan(api, "owner-job")
    second = plan(api, "competing-job")
    assert approve(api, first).status_code == 200
    assert approve(api, second).status_code == 409
    stepped = client.post(f"/api/routes/{first['route_id']}/execute",
        headers=headers, json={"stop_after_segments": 1})
    assert stepped.status_code == 200, stepped.text
    assert instance.state_machine.get_state(first["job_id"]) == ExecutionState.PAUSED
    assert approve(api, second).status_code == 409
    finished = client.post(f"/api/routes/{first['route_id']}/resume", headers=headers, json={})
    assert finished.status_code == 200, finished.text
    assert instance.state_machine.get_state(first["job_id"]) == ExecutionState.COMPLETED
    assert approve(api, second).status_code == 200


def test_unapproved_proposals_do_not_reserve_a_machine(api):
    first = plan(api, "proposal-first")
    second = plan(api, "proposal-second")
    assert approve(api, second).status_code == 200
    assert approve(api, first).status_code == 409


def test_concurrent_approvals_reserve_one_job_without_approving_the_loser(api):
    instance, _, _ = api
    proposals = [plan(api, "concurrent-first"), plan(api, "concurrent-second")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda route: approve(api, route), proposals))
    assert sorted(response.status_code for response in responses) == [200, 409]
    statuses = [instance.routes_by_id[route["route_id"]].status.value for route in proposals]
    assert sorted(statuses) == ["APPROVED", "PROPOSED"]


def test_a_stopped_machine_does_not_block_a_different_machine(api):
    _, client, headers = api
    stopped = plan(api, "stopped-sweeper")
    response = client.post("/api/events", headers=headers, json={
        "job_id": stopped["job_id"], "route_id": stopped["route_id"],
        "event_type": "EMERGENCY_STOP",
    })
    assert response.status_code == 200, response.text
    heavy = plan(api, "other-machine", "heavy-utility-02")
    assert approve(api, heavy).status_code == 200
    response = client.post(f"/api/routes/{heavy['route_id']}/execute",
        headers=headers, json={"stop_after_segments": 1})
    assert response.status_code == 200, response.text


def test_mismatched_event_job_and_route_do_not_stop_a_different_job(api):
    instance, client, headers = api
    route = plan(api, "actual-job")
    assert approve(api, route).status_code == 200
    response = client.post("/api/events", headers=headers, json={
        "job_id": "unrelated-job", "route_id": route["route_id"],
        "event_type": "EMERGENCY_STOP",
    })
    assert response.status_code == 409, response.text
    assert instance.state_machine.get_state(route["job_id"]) == ExecutionState.READY
    assert instance.state_machine.get_state("unrelated-job") == ExecutionState.IDLE
    response = client.post(f"/api/routes/{route['route_id']}/execute",
        headers=headers, json={"stop_after_segments": 1})
    assert response.status_code == 200, response.text


def test_only_existing_trusted_hardware_reset_can_clear_the_machine_latch():
    states = ExecutionStateMachine(AuditAndEventManager())
    states.register_job_machine("stopped", "machine-a")
    states.transition("stopped", ExecutionState.EMERGENCY_STOP, "operator", "test stop")
    with pytest.raises(SafetyInterlockError, match="emergency stop"):
        states.assert_machine_available("machine-a", "another-job")
    for role, hardware_cleared in ((OperatorRole.OPERATOR, True), (OperatorRole.SUPERVISOR, False)):
        with pytest.raises(SafetyInterlockError):
            states.transition("stopped", ExecutionState.IDLE, "test", "reset attempt",
                role=role, hardware_estop_cleared=hardware_cleared)
    states.transition("stopped", ExecutionState.IDLE, "trusted-adapter", "confirmed hardware reset",
        role=OperatorRole.SUPERVISOR, hardware_estop_cleared=True)
    states.assert_machine_available("machine-a", "another-job")


def test_machine_binding_cannot_be_changed_to_escape_existing_state():
    states = ExecutionStateMachine(AuditAndEventManager())
    states.register_job_machine("job", "machine-a")
    with pytest.raises(SafetyInterlockError, match="bound machine"):
        states.register_job_machine("job", "machine-b")
