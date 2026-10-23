"""Cancelling work releases a machine without resetting an emergency stop."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from .execution import SafetyInterlockError
from .models import ExecutionState, OperatorRole
from .test_machine_interlocks import api, approve, plan


def cancel(api, route):
    _, client, headers = api
    return client.post(f"/api/routes/{route['route_id']}/cancel", headers=headers)


def test_cancel_paused_work_preserves_completed_progress_and_releases_machine(api):
    instance, client, headers = api
    source = plan(api, "cancel-paused")
    assert approve(api, source).status_code == 200
    stepped = client.post(f"/api/routes/{source['route_id']}/execute", headers=headers,
        json={"stop_after_segments": 1})
    assert stepped.status_code == 200, stepped.text
    route = instance.routes_by_id[source["route_id"]]
    completed = [segment.model_dump() for segment in route.segments if segment.completed]
    frames = list(instance.simulator.telemetry_store[route.route_id])
    competing = plan(api, "after-cancel")
    assert approve(api, competing).status_code == 409
    response = cancel(api, source)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ABORTED"
    assert instance.state_machine.get_state(source["job_id"]) == ExecutionState.ABORTED
    assert [segment.model_dump() for segment in route.segments if segment.completed] == completed
    assert instance.simulator.telemetry_store[route.route_id] == frames
    assert approve(api, competing).status_code == 200
    assert client.post(f"/api/routes/{route.route_id}/resume", headers=headers, json={}).status_code == 409
    assert cancel(api, source).status_code == 200
    assert sum(entry.action == "CANCEL_JOB" and entry.job_id == source["job_id"]
               for entry in instance.audit._audit_log) == 1


@pytest.mark.parametrize("approved", [False, True])
def test_cancel_waiting_or_ready_work_allows_a_new_job(api, approved):
    source = plan(api, "cancel-before-start")
    if approved:
        assert approve(api, source).status_code == 200
    response = cancel(api, source)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ABORTED"
    assert approve(api, plan(api, "replacement-before-start")).status_code == 200


def test_cancel_cannot_clear_emergency_stop_even_via_a_new_job(api):
    instance, client, headers = api
    stopped = plan(api, "stopped-before-cancel")
    assert approve(api, stopped).status_code == 200
    response = client.post("/api/events", headers=headers, json={
        "job_id": stopped["job_id"], "route_id": stopped["route_id"], "event_type": "EMERGENCY_STOP",
    })
    assert response.status_code == 200, response.text
    assert cancel(api, stopped).status_code == 409
    assert instance.state_machine.get_state(stopped["job_id"]) == ExecutionState.EMERGENCY_STOP
    unrelated = plan(api, "new-job-on-stopped-machine")
    assert cancel(api, unrelated).status_code == 409
    assert instance.routes_by_id[unrelated["route_id"]].status.value == "PROPOSED"
    assert approve(api, unrelated).status_code == 409


def test_stale_route_cancel_targets_the_same_jobs_current_revision(api):
    instance, client, headers = api
    old = plan(api, "cancel-latest-revision")
    current = plan(api, old["job_id"])
    assert current["route_id"] != old["route_id"]
    assert instance.routes_by_id[old["route_id"]].status.value == "SUPERSEDED"
    assert approve(api, current).status_code == 200
    response = cancel(api, old)
    assert response.status_code == 200, response.text
    assert response.json()["route_id"] == current["route_id"]
    assert response.json()["status"] == "ABORTED"
    assert instance.routes_by_id[old["route_id"]].status.value == "SUPERSEDED"


def test_cancel_completed_run_keeps_its_results(api):
    instance, client, headers = api
    source = plan(api, "completed-before-cancel")
    assert approve(api, source).status_code == 200
    finished = client.post(f"/api/routes/{source['route_id']}/execute", headers=headers, json={})
    assert finished.status_code == 200, finished.text
    route = instance.routes_by_id[source["route_id"]]
    before = route.model_dump()
    response = cancel(api, source)
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert route.model_dump() == before
    assert all(segment.completed for segment in route.segments)


def test_cancelled_in_progress_search_cannot_publish_after_it_returns(api, monkeypatch):
    instance, client, headers = api
    source = plan(api, "cancel-during-search")
    assert approve(api, source).status_code == 200
    from .models import EnvironmentMapData, PlanRequest
    from .test_depot_mission import point, polygon
    request = PlanRequest(job_id=source["job_id"], use_rl=False, enable_depot_cycles=True,
        preserve_completed=True,
        operating_boundary=polygon([(0, 0), (18, 0), (18, 15), (0, 15)]),
        start_location=point(2, 2), disposal_location=point(12, 2),
        environment_map=EnvironmentMapData(map_id="cancel-search-test"))
    entered, release = Event(), Event()
    original = instance._make_candidate

    def blocked_candidate(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10), "The cancellation test did not release its planning worker."
        return original(*args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", blocked_candidate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(instance.plan_and_optimize, request)
        try:
            assert entered.wait(timeout=5)
            cancelled = cancel(api, source)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "ABORTED"
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(SafetyInterlockError, match="(?i)cancel|abort|interrupt"):
            pending.result(timeout=10)
    assert instance.state_machine.get_state(source["job_id"]) == ExecutionState.ABORTED
    assert instance._latest_route_by_job[source["job_id"]] == source["route_id"]
    assert instance.routes_by_id[source["route_id"]].status.value == "ABORTED"
    assert approve(api, plan(api, "new-job-after-cancelled-search")).status_code == 200


def test_cancel_requires_human_role_and_reports_missing_route(api, monkeypatch):
    from .config import settings
    instance, client, headers = api
    route = plan(api, "human-cancellation-only")
    monkeypatch.setattr(settings, "internal_service_token", "cancel-service-token")
    response = client.post(f"/api/routes/{route['route_id']}/cancel",
        headers={"Authorization": "Bearer cancel-service-token"})
    assert response.status_code == 403
    assert instance.state_machine.get_state(route["job_id"]) == ExecutionState.AWAITING_APPROVAL
    assert client.post("/api/routes/unknown-route/cancel", headers=headers).status_code == 404
