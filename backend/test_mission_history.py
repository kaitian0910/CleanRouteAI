"""Local route feedback survives restart without changing policy weights."""
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from .config import KPIPolicyConfig, settings
from .mission_history import MissionHistory, layout_fingerprint
from .models import (
    Coordinate, EnvironmentMapData, ExclusionZone, GeoPolygon, KPIEvaluationResponse,
    MachineProfile, PlanRequest, RouteCandidate, RouteSegment, TaskItem, TaskType,
)
from .orchestrator import AgenticWorkflowOrchestrator


@pytest.fixture
def history_directory():
    # Python 3.14's mode-0700 temp directories are unreadable under this host's
    # Windows ACL mapping. Keep ordinary, isolated scratch files in the workspace.
    root = Path(__file__).resolve().parent
    scratch = root / f".history-test-{uuid4().hex}"
    scratch.mkdir(mode=0o777)
    try:
        yield scratch
    finally:
        assert scratch.resolve().parent == root
        shutil.rmtree(scratch)


def point(x=0, y=0):
    return Coordinate.from_local_meters(x, y, Coordinate(lat=3.14, lon=101.68))


def polygon():
    return GeoPolygon(vertices=[point(), point(30), point(30, 15), point(0, 15)])


def workload():
    return PlanRequest(enable_depot_cycles=True, use_rl=False, operating_boundary=polygon(),
        start_location=point(2, 2), disposal_location=point(2, 2),
        tasks=[TaskItem(task_type=TaskType.RUBBISH_COLLECTION, location=point(10, 8))],
        exclusion_zones=[ExclusionZone(polygon=GeoPolygon(vertices=[
            point(20, 5), point(23, 5), point(23, 8), point(20, 8)]))],
        environment_map=EnvironmentMapData(map_id="history-test"))


def planned_route(**values):
    return RouteCandidate(job_id="test-history", route_version=1, machine_id="test",
        task_mode=TaskType.AREA_SWEEP, origin=point(), disposal_depot=point(),
        kpi_evaluation=KPIEvaluationResponse(route_version=1, policy_version="test", passed=True,
            score=100, metrics={}, failed_constraints=[], recommended_adjustments=[], requires_human_review=False), **values)


def test_feasible_history_and_actual_completion_survive_disk_reload(history_directory):
    path = history_directory / "history.json"
    history = MissionHistory(path)
    route = planned_route(estimated_completion_minutes=61, sweep_angle_deg=90)
    history.record_plan(route, "same-workload")
    history.record_plan(route, "same-workload")
    assert len(history.records()) == 1
    reloaded = MissionHistory(path)
    assert reloaded.preferred_angle("same-workload") == 90
    assert reloaded.records()[0]["actual_completion_minutes"] is None
    reloaded.record_completion(route, 64.75)
    actual = MissionHistory(path).records()[0]
    assert actual["status"] == "COMPLETED_SIMULATION"
    assert actual["estimated_completion_minutes"] == 61
    assert actual["actual_completion_minutes"] == 64.75
    assert actual["mission_elapsed_minutes"] == 64.75


def test_failed_plans_and_nonmatching_workloads_do_not_warm_start():
    history = MissionHistory()
    blocked = planned_route(estimated_completion_minutes=1, sweep_angle_deg=135)
    blocked.kpi_evaluation.passed = False
    history.record_plan(blocked, "matching")
    history.record_plan(planned_route(estimated_completion_minutes=45, sweep_angle_deg=90), "matching")
    history.record_plan(planned_route(estimated_completion_minutes=40, sweep_angle_deg=45), "matching")
    history.record_plan(planned_route(estimated_completion_minutes=1, sweep_angle_deg=135), "different")
    assert history.preferred_angle("matching") == 45
    assert history.preferred_angle("unseen") is None
    assert len(history.records("test")) == 3
    assert history.records("another-machine") == []
    assert history.path is None


def test_fingerprint_ignores_generated_ids_but_preserves_constraints():
    request = workload()
    machine = MachineProfile(machine_id="test", name="test")
    policy = KPIPolicyConfig()
    fingerprint = lambda req=request, robot=machine, gate=policy: layout_fingerprint(req, robot, gate, req.environment_map)
    original = fingerprint()
    regenerated = workload()
    assert regenerated.job_id != request.job_id
    assert regenerated.tasks[0].task_id != request.tasks[0].task_id
    assert regenerated.exclusion_zones[0].zone_id != request.exclusion_zones[0].zone_id
    assert fingerprint(regenerated) == original
    variants = []
    changed = request.model_copy(deep=True)
    changed.exclusion_zones[0].polygon.vertices[0] = point(19, 5)
    variants.append(changed)
    changed = request.model_copy(deep=True)
    changed.disposal_location = point(4, 2)
    variants.append(changed)
    changed = request.model_copy(deep=True)
    changed.tasks[0].service_duration_sec += 30
    variants.append(changed)
    changed = request.model_copy(deep=True)
    changed.tasks[0].time_window_end_min = 100
    variants.append(changed)
    changed = request.model_copy(deep=True)
    changed.enable_depot_cycles = False
    variants.append(changed)
    changed = request.model_copy(deep=True)
    changed.mission_elapsed_min = 45
    variants.append(changed)
    changed = request.model_copy(deep=True)
    changed.tasks[0].completed = True
    variants.append(changed)
    assert all(fingerprint(changed) != original for changed in variants)
    low_battery = machine.model_copy(update={"current_battery_percent": 40})
    assert fingerprint(robot=low_battery) != original
    assert fingerprint(gate=policy.model_copy(update={"max_estimated_duration_minutes": 60})) != original


def test_fingerprint_compares_completed_coverage_without_generated_segment_ids():
    request = workload()
    machine = MachineProfile(machine_id="test", name="test")
    policy = KPIPolicyConfig()
    completed = RouteSegment(segment_type="SWEEP_SWATH", waypoints=[point(2, 2), point(15, 2)],
        distance_m=13, estimated_duration_sec=13, swath_width_m=1.6, completed=True)
    fingerprint = lambda segments: layout_fingerprint(request, machine, policy, request.environment_map,
        completed_segments=segments)
    baseline = fingerprint([completed])
    regenerated = completed.model_copy(update={"segment_id": "regenerated-id"})
    assert fingerprint([regenerated]) == baseline
    assert fingerprint([]) != baseline
    different_coverage = completed.model_copy(update={"waypoints": [point(2, 3), point(15, 3)]}, deep=True)
    assert fingerprint([different_coverage]) != baseline
    unfinished = completed.model_copy(update={"completed": False})
    assert fingerprint([unfinished]) == fingerprint([])


@pytest.fixture
def isolated_api(monkeypatch):
    from . import main
    instance = AgenticWorkflowOrchestrator()
    monkeypatch.setattr(main, "orchestrator", instance)
    monkeypatch.setattr(settings, "operator_token", "history-test-token")
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings.optimization, "max_iterations", 2)
    return instance, TestClient(main.app), {"Authorization": "Bearer history-test-token"}


def test_history_api_reports_selected_plan_then_simulated_completion(isolated_api):
    instance, client, headers = isolated_api
    request = workload()
    request.tasks = []
    request.exclusion_zones = []
    response = client.post("/api/routes/plan", json=request.model_dump(mode="json"), headers=headers)
    assert response.status_code == 200, response.text
    route = response.json()
    assert route["kpi_evaluation"]["passed"], route["kpi_evaluation"]
    records = client.get("/api/feedback/history", headers=headers).json()["records"]
    assert len(records) == 1
    assert records[0]["route_id"] == route["route_id"]
    assert records[0]["status"] == "PLANNED_FEASIBLE"
    assert records[0]["actual_completion_minutes"] is None
    assert client.post(f"/api/routes/{route['route_id']}/approve", headers=headers).status_code == 200
    executed = client.post(f"/api/routes/{route['route_id']}/execute", headers=headers)
    assert executed.status_code == 200, executed.text
    assert executed.json()["execution_state"] == "COMPLETED"
    actual = client.get("/api/feedback/history", headers=headers).json()["records"][0]
    assert actual["status"] == "COMPLETED_SIMULATION"
    assert actual["actual_completion_minutes"] > 0
    frames = instance.simulator.telemetry_store[route["route_id"]]
    assert actual["actual_completion_minutes"] == frames[-1].mission_elapsed_min
    assert instance.mission_history.path is None
    filtered = client.get("/api/feedback/history?machine_id=absent", headers=headers)
    assert filtered.json()["records"] == []
    assert client.get("/api/feedback/history").status_code in (401, 403)


def test_warm_start_is_reevaluated_for_matching_workload(isolated_api, monkeypatch):
    instance, _, _ = isolated_api
    request = workload()
    request.tasks = []
    request.exclusion_zones = []
    first = instance.plan_and_optimize(request, actor="test")
    record = instance.mission_history.records()[0]
    remembered = planned_route(estimated_completion_minutes=0.01, sweep_angle_deg=90)
    instance.mission_history.record_plan(remembered, record["layout_fingerprint"])
    calls = []
    original = instance._make_candidate
    def capture(*args, **kwargs):
        calls.append(list(args[5]))
        return original(*args, **kwargs)
    monkeypatch.setattr(instance, "_make_candidate", capture)
    repeated = request.model_copy(deep=True)
    repeated.job_id = "same-workload-next-day"
    selected = instance.plan_and_optimize(repeated, actor="test")
    assert calls[0][0] == 90
    assert selected.kpi_evaluation.passed
    assert selected.route_id != first.route_id
    assert any("reevaluated as a warm start" in reason for reason in selected.candidate_rationale)


def test_corrupted_file_is_exposed_in_history_api(isolated_api, history_directory):
    instance, client, headers = isolated_api
    corrupted = history_directory / "bad.json"
    corrupted.write_text("{broken-json", encoding="utf-8")
    instance.mission_history = MissionHistory(corrupted)
    response = client.get("/api/feedback/history", headers=headers)
    assert response.status_code == 200
    assert response.json()["records"] == []
    assert "Could not read mission history" in response.json()["detail"]


@pytest.mark.parametrize("contents", [{"unexpected": 1}, [None, {"route_id": "partial"}]])
def test_wrong_schema_is_reported_without_becoming_warm_start(history_directory, contents):
    path = history_directory / "bad-schema.json"
    path.write_text(json.dumps(contents), encoding="utf-8")
    history = MissionHistory(path)
    assert history.records() == []
    assert history.last_error is not None
    assert history.preferred_angle("anything") is None


def test_schema_loader_keeps_valid_records_and_rejects_nonfinite_values(history_directory):
    path = history_directory / "mixed.json"
    history = MissionHistory(path)
    history.record_plan(planned_route(estimated_completion_minutes=30, sweep_angle_deg=90), "matching")
    valid = history.records()[0]
    invalid = dict(valid, route_id="bad", estimated_completion_minutes=float("nan"))
    path.write_text(json.dumps([valid, invalid, {"route_id": "incomplete"}]), encoding="utf-8")
    reloaded = MissionHistory(path)
    assert len(reloaded.records()) == 1
    assert reloaded.preferred_angle("matching") == 90
    assert "skipped 2 invalid record" in reloaded.last_error
