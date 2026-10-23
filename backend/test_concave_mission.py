"""Regression from the six-bin, concave-area route reported by the operator."""
import json
from pathlib import Path
from time import monotonic

from shapely.geometry import LineString

from backend.config import settings
from backend.models import OperatorRole, PlanRequest
from backend.orchestrator import AgenticWorkflowOrchestrator


def test_reported_concave_six_bin_job_plans_and_completes_without_boundary_shortcut(tmp_path, monkeypatch):
    data = json.loads((Path(__file__).parent / "examples" / "concave_six_bin_regression.json").read_text())
    request = PlanRequest.model_validate(data)
    monkeypatch.setattr(settings, "enable_default_campus_terrain_layer", True)
    engine = AgenticWorkflowOrchestrator(history_path=tmp_path / "history.json")
    engine.routing_engine._force_offline_for_test = True
    collection_calls = []
    compute = engine.routing_engine.compute_matrix_and_paths

    def capture(*args, **kwargs):
        collection_calls.append(1)
        return compute(*args, **kwargs)

    monkeypatch.setattr(engine.routing_engine, "compute_matrix_and_paths", capture)
    started = monotonic()
    route = engine.plan_and_optimize(request)
    assert route.kpi_evaluation.passed, route.route_safety_issues
    assert not route.route_safety_issues
    assert len(route.task_schedule) == 6
    assert len(route.rl_feedback) > 1
    assert len(collection_calls) == 1  # Not rebuilt for each sweep alternative.
    boundary = request.operating_boundary.to_shapely_local(request.operating_boundary.vertices[0]).buffer(.03)
    machine = engine.machines[request.machine_id]
    for segment in route.segments:
        if segment.segment_type == "TRANSIT" and segment.target_task_id:
            geometry = LineString([point.to_local_meters(request.operating_boundary.vertices[0]) for point in segment.waypoints])
            assert boundary.covers(geometry.buffer(machine.clearance_width_m / 2)), segment.segment_id
    engine.approve_route(route.route_id, "test", OperatorRole.OPERATOR)
    result = engine.simulator.execute_simulation(route, machine)
    assert all(task.completed for task in route.tasks)
    assert all(segment.completed for segment in route.segments)
    print(f"Exact reported job planned and simulated in {monotonic() - started:.2f}s")
