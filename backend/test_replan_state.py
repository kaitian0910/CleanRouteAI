"""Browser-style replans must resume the backend's recorded mission progress."""

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.models import Coordinate, PlanRequest, TaskItem, TaskType
from backend.orchestrator import AgenticWorkflowOrchestrator


@pytest.fixture
def api(monkeypatch):
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    monkeypatch.setattr(main, "orchestrator", instance)
    with TestClient(main.app) as client:
        client.headers["Authorization"] = "Bearer test-operator-token"
        yield client, instance


@pytest.mark.parametrize("include_finished_task", [True, False])
def test_plan_api_preserves_recorded_collection_progress(api, include_finished_task):
    client, instance = api
    req = PlanRequest(
        job_id="browser-replan",
        start_location=Coordinate(lat=3.1390, lon=101.6869),
        tasks=[
            TaskItem(task_id="bin-a", task_type=TaskType.RUBBISH_COLLECTION,
                     location=Coordinate(lat=3.1391, lon=101.6870)),
            TaskItem(task_id="bin-b", task_type=TaskType.RUBBISH_COLLECTION,
                     location=Coordinate(lat=3.1392, lon=101.6871)),
        ],
    )
    response = client.post("/api/routes/plan", json=req.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    route_id = response.json()["route_id"]
    assert client.post(f"/api/routes/{route_id}/approve").status_code == 200
    route = instance.routes_by_id[route_id]
    first_stop = next(i for i, s in enumerate(route.segments) if s.segment_type == "COLLECTION_STOP")
    assert client.post(f"/api/routes/{route_id}/execute", json={"stop_after_segments": first_stop + 1}).status_code == 200
    frame = instance.simulator.telemetry_store[route_id][-1]
    completed_ids = {s.segment_id for s in route.segments if s.completed}
    completed_task_id = next(t.task_id for t in route.tasks if t.completed)
    machine = instance.machines[route.machine_id]
    battery = machine.current_battery_percent
    assert battery < 95.0

    # A browser may still have its original task list and start point.
    req.preserve_completed = True
    for task in req.tasks:
        if task.task_id != completed_task_id:
            task.completed = True  # Client flags cannot claim unexecuted work.
    if not include_finished_task:
        req.tasks = [t for t in req.tasks if t.task_id != completed_task_id]
    response = client.post("/api/routes/plan", json=req.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    revised = instance.routes_by_id[response.json()["route_id"]]
    assert revised.kpi_evaluation.passed
    assert revised.origin == frame.position
    assert revised.disposal_depot == route.disposal_depot
    assert revised.mission_elapsed_min == frame.mission_elapsed_min
    assert machine.current_battery_percent == battery
    assert completed_ids <= {s.segment_id for s in revised.segments if s.completed}
    assert next(t for t in revised.tasks if t.task_id == completed_task_id).completed
    assert not any(s.target_task_id == completed_task_id and not s.completed for s in revised.segments)
    assert any(not task.completed for task in revised.tasks)
    assert revised.replan_comparison.previous_route_id == route_id

    # Replanning again before the revised route moves must not lose elapsed time.
    repeated = client.post("/api/routes/plan", json=req.model_dump(mode="json"))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["mission_elapsed_min"] == frame.mission_elapsed_min
    assert repeated.json()["origin"] == frame.position.model_dump()


def test_plan_api_rejects_preserving_an_unknown_job(api):
    client, _ = api
    response = client.post("/api/routes/plan", json={"job_id": "missing", "preserve_completed": True})
    assert response.status_code == 422
    assert "no previous route" in response.json()["detail"]
