"""A route revision is one published plan, rather than each search alternative."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.config import settings
from backend.execution import SafetyInterlockError
from backend.models import ExecutionState, ExclusionZone, OperatorRole, OperationalEventRequest
from backend.test_completion_feedback import request
from backend.test_depot_mission import polygon, workflow


@pytest.fixture
def revision_api(monkeypatch):
    instance = workflow()
    monkeypatch.setattr(main, "orchestrator", instance)
    monkeypatch.setattr(settings.optimization, "max_iterations", 3)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "operator_token", "revision-test-token")
    client = TestClient(main.app)
    client.headers["Authorization"] = "Bearer revision-test-token"
    return instance, client


def test_alternatives_share_one_revision_but_have_distinct_candidate_identity(revision_api):
    instance, _ = revision_api
    route = instance.plan_and_optimize(request())
    options = instance.job_route_history[route.job_id]

    assert len(options) > 1
    assert route.route_version == 1
    assert {option.route_version for option in options} == {1}
    assert {option.plan_epoch for option in options} == {1}
    assert len({option.route_id for option in options}) == len(options)
    assert [option.candidate_index for option in options] == list(range(1, len(options) + 1))
    assert route.planning_run_id
    assert {option.planning_run_id for option in options} == {route.planning_run_id}
    assert [option.route_id for option in options if option.is_selected_candidate] == [route.route_id]
    assert instance._latest_route_by_job[route.job_id] == route.route_id


def test_replanning_advances_revision_once_and_history_separates_options(revision_api):
    instance, client = revision_api
    original_request = request()
    first = instance.plan_and_optimize(original_request)
    revised_request = original_request.model_copy(deep=True)
    revised_request.preserve_completed = True
    revised = instance.plan_and_optimize(revised_request)

    assert revised.route_version == first.route_version + 1 == 2
    assert revised.planning_run_id != first.planning_run_id
    options = instance.job_route_history[first.job_id]
    for revision in (1, 2):
        batch = [option for option in options if option.route_version == revision]
        assert len(batch) > 1
        assert sum(option.is_selected_candidate for option in batch) == 1
    response = client.get(f"/api/routes/{revised.route_id}/metrics")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [entry["version"] for entry in payload["history_versions"]] == [1, 2]
    assert [entry["route_id"] for entry in payload["history_versions"]] == [first.route_id, revised.route_id]
    assert {entry["route_id"] for entry in payload["candidate_options"]} == {option.route_id for option in options}
    assert len(payload["candidate_options"]) > len(payload["history_versions"])


def test_only_selected_option_can_be_approved_and_old_revision_stays_stale(revision_api):
    instance, client = revision_api
    original_request = request()
    first = instance.plan_and_optimize(original_request)
    rejected = next(option for option in instance.job_route_history[first.job_id]
                    if option.route_id != first.route_id)
    assert rejected.route_version == first.route_version
    assert client.post(f"/api/routes/{rejected.route_id}/approve").status_code == 409
    assert rejected.approved_by is None
    assert client.post(f"/api/routes/{first.route_id}/approve").status_code == 200

    original_request.preserve_completed = True
    revised = instance.plan_and_optimize(original_request)
    assert revised.route_version == first.route_version + 1
    assert client.post(f"/api/routes/{first.route_id}/approve").status_code == 409
    assert client.post(f"/api/routes/{revised.route_id}/approve").status_code == 200
    assert instance._latest_route_by_job[first.job_id] == revised.route_id


def test_internal_comparisons_do_not_publish_intermediate_options(revision_api, monkeypatch):
    instance, _ = revision_api
    make_candidate = instance._make_candidate
    inspected = []

    def inspect_publication(*args, **kwargs):
        submitted = args[0]
        inspected.append(instance._latest_route_by_job.get(submitted.job_id))
        assert instance._latest_route_by_job.get(submitted.job_id) is None
        assert not any(option.is_selected_candidate
                       for option in instance.job_route_history.get(submitted.job_id, []))
        return make_candidate(*args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", inspect_publication)
    selected = instance.plan_and_optimize(request())
    assert len(inspected) > 1
    assert instance._latest_route_by_job[selected.job_id] == selected.route_id


def test_replan_keeps_previous_route_current_until_one_new_selection_is_published(revision_api, monkeypatch):
    instance, _ = revision_api
    submitted = request()
    first = instance.plan_and_optimize(submitted)
    make_candidate = instance._make_candidate
    inspected = []

    def inspect_publication(*args, **kwargs):
        inspected.append(instance._latest_route_by_job.get(first.job_id))
        assert instance._latest_route_by_job[first.job_id] == first.route_id
        new_options = [option for option in instance.job_route_history[first.job_id]
                       if option.route_version > first.route_version]
        assert not any(option.is_selected_candidate for option in new_options)
        return make_candidate(*args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", inspect_publication)
    submitted.preserve_completed = True
    revised = instance.plan_and_optimize(submitted)
    assert len(inspected) > 1
    assert revised.route_version == first.route_version + 1
    assert instance._latest_route_by_job[first.job_id] == revised.route_id
    assert [option.route_id for option in instance.job_route_history[first.job_id]
            if option.route_version == revised.route_version and option.is_selected_candidate] == [revised.route_id]


def test_simultaneous_same_job_plan_is_rejected_without_replacing_the_search(revision_api, monkeypatch):
    instance, client = revision_api
    submitted = request()
    entered = Event()
    release = Event()
    make_candidate = instance._make_candidate

    def slow_candidate(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("The concurrent planning test did not release its worker.")
        return make_candidate(*args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", slow_candidate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(instance.plan_and_optimize, submitted)
        try:
            assert entered.wait(timeout=5)
            rejected = client.post("/api/routes/plan", json=submitted.model_dump(mode="json"))
            assert rejected.status_code == 409, rejected.text
            assert instance.state_machine.get_state(submitted.job_id) == ExecutionState.PLANNING
        finally:
            release.set()
        selected = pending.result(timeout=10)

    assert selected.route_version == 1
    assert instance._latest_route_by_job[selected.job_id] == selected.route_id
    assert {option.planning_run_id for option in instance.job_route_history[selected.job_id]} == {selected.planning_run_id}


def test_emergency_stop_during_comparison_cannot_publish_or_start_a_route(revision_api, monkeypatch):
    instance, _ = revision_api
    submitted = request()
    make_candidate = instance._make_candidate
    calls = 0

    def stop_during_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            instance.handle_operational_event(
                OperationalEventRequest(job_id=submitted.job_id, event_type="EMERGENCY_STOP"),
                actor="test-operator", role=OperatorRole.OPERATOR,
            )
        return make_candidate(*args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", stop_during_search)
    with pytest.raises(SafetyInterlockError, match="(?i)emergency|stop"):
        instance.plan_and_optimize(submitted)

    assert calls == 2
    assert instance.state_machine.get_state(submitted.job_id) == ExecutionState.EMERGENCY_STOP
    assert submitted.job_id not in instance._latest_route_by_job
    assert not any(option.is_selected_candidate
                   for option in instance.job_route_history.get(submitted.job_id, []))
    assert not any(option.approved_by for option in instance.routes_by_id.values())
    assert not instance.simulator.telemetry_store


def test_emergency_stop_request_is_not_blocked_behind_slow_planning(revision_api, monkeypatch):
    instance, client = revision_api
    submitted = request()
    entered = Event()
    release = Event()
    make_candidate = instance._make_candidate

    def slow_candidate(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("The emergency-stop test did not release its planning worker.")
        return make_candidate(*args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", slow_candidate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(instance.plan_and_optimize, submitted)
        try:
            assert entered.wait(timeout=5)
            stopping = executor.submit(client.post, "/api/events", json={
                "job_id": submitted.job_id, "event_type": "EMERGENCY_STOP",
            })
            response = stopping.result(timeout=3)
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "EMERGENCY_STOP"
            assert not pending.done()
            assert instance.state_machine.get_state(submitted.job_id) == ExecutionState.EMERGENCY_STOP
        finally:
            release.set()
        with pytest.raises(SafetyInterlockError, match="(?i)emergency|stop"):
            pending.result(timeout=10)

    assert submitted.job_id not in instance._latest_route_by_job
    assert not instance.simulator.telemetry_store


def test_obstacle_replan_preserves_operator_limits_and_learning_preference(revision_api):
    instance, _ = revision_api
    submitted = request()
    submitted.kpi_policy_override = {
        "max_estimated_duration_minutes": 120,
        "min_energy_reserve_percent": 35,
        "max_travel_distance_m": 10000,
    }
    first = instance.plan_and_optimize(submitted)
    assert first.kpi_evaluation.passed
    machine = instance.machines[first.machine_id]
    machine.current_battery_percent = 75.0
    result = instance.handle_operational_event(
        OperationalEventRequest(
            job_id=first.job_id, route_id=first.route_id, event_type="OBSTACLE_DETECTED",
            new_exclusion_zone=ExclusionZone(name="New obstacle", polygon=polygon([
                (50, 70), (55, 70), (55, 75), (50, 75),
            ])),
        ),
        actor="test-operator", role=OperatorRole.OPERATOR,
    )
    revised = result["candidate"]
    assert revised.route_version == first.route_version + 1
    assert revised.use_rl is first.use_rl is False
    assert revised.kpi_policy_snapshot == first.kpi_policy_snapshot
    assert revised.kpi_evaluation.passed, revised.kpi_evaluation.failed_constraints
    assert revised.initial_battery_percent == machine.current_battery_percent == 75.0
    assert revised.minimum_battery_percent >= submitted.kpi_policy_override["min_energy_reserve_percent"]
