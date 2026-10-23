"""Urgent bin windows must survive an hours-long hybrid sweeping mission."""
import pytest
from shapely.geometry import LineString

from backend.models import (
    Coordinate, EnvironmentMapData, ExecutionState, ExclusionZone, GeoPolygon,
    OperatorRole, PlanRequest, TaskItem, TaskType,
)
from backend.orchestrator import AgenticWorkflowOrchestrator


ORIGIN = Coordinate(lat=3.139, lon=101.6869)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


def mission(job_id):
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    request = PlanRequest(
        job_id=job_id, enable_depot_cycles=True, use_rl=False,
        start_location=point(3, 3), disposal_location=point(195, 3),
        operating_boundary=polygon([(0, 0), (200, 0), (200, 225), (0, 225)]),
        environment_map=EnvironmentMapData(map_id="clear-hybrid-test-terrain"),
        tasks=[TaskItem(task_id=f"urgent-bin-{index}", task_type=TaskType.RUBBISH_COLLECTION,
                        location=point(x, y), service_duration_sec=60,
                        time_window_end_min=90)
               for index, (x, y) in enumerate([(12, 12), (80, 35), (170, 45)])],
    )
    return instance, request


def assert_feasible_hybrid(route):
    assert route.task_mode == TaskType.HYBRID
    assert route.kpi_evaluation.passed, (route.kpi_evaluation.failed_constraints, route.skipped_tasks, route.route_safety_issues)
    assert not route.route_safety_issues
    assert route.estimated_completion_minutes > 90
    assert route.depot_visits > 0
    assert not route.skipped_tasks
    scheduled = {entry.task_id for entry in route.task_schedule}
    assert {task.task_id for task in route.tasks if not task.completed} <= scheduled
    assert scheduled <= {task.task_id for task in route.tasks}
    assert all(entry.service_end_min <= 90 for entry in route.task_schedule)
    assert any(segment.segment_type == "SWEEP_SWATH" and not segment.completed for segment in route.segments)


def test_large_hybrid_collects_urgent_bins_and_finishes_all_sweeping():
    instance, request = mission("large-urgent-hybrid")
    route = instance.plan_and_optimize(request, actor="test")
    assert_feasible_hybrid(route)
    instance.approve_route(route.route_id, actor="test", role=OperatorRole.OPERATOR)
    frames = instance.simulator.execute_simulation(route, instance.machines[route.machine_id])
    assert all(task.completed for task in route.tasks)
    assert all(segment.completed for segment in route.segments)
    assert instance.state_machine.get_state(route.job_id) == ExecutionState.COMPLETED
    assert frames[-1].position == route.disposal_depot
    assert frames[-1].mission_elapsed_min == pytest.approx(route.estimated_completion_minutes, abs=0.01)
    assert min(frame.battery_percent for frame in frames) >= 20


def test_large_hybrid_obstacle_replan_preserves_collected_bin_and_finishes_remaining_work():
    instance, request = mission("obstacle-urgent-hybrid")
    route = instance.plan_and_optimize(request, actor="test")
    assert_feasible_hybrid(route)
    instance.approve_route(route.route_id, actor="test", role=OperatorRole.OPERATOR)
    first_collection = next(index for index, segment in enumerate(route.segments)
                            if segment.segment_type == "COLLECTION_STOP")
    machine = instance.machines[route.machine_id]
    frame = instance.simulator.execute_simulation(route, machine, stop_after_segments=first_collection + 1)[-1]
    completed = {segment.segment_id: segment.model_dump() for segment in route.segments if segment.completed}
    completed_tasks = {task.task_id for task in route.tasks if task.completed}
    request.preserve_completed = True
    obstacle = ExclusionZone(name="New central obstacle",
                             polygon=polygon([(90, 100), (102, 100), (102, 114), (90, 114)]))
    request.exclusion_zones = [obstacle]
    revised = instance.plan_and_optimize(request, actor="test")
    assert_feasible_hybrid(revised)
    assert revised.origin == frame.position
    assert revised.mission_elapsed_min == frame.mission_elapsed_min
    assert {segment.segment_id: segment.model_dump() for segment in revised.segments if segment.completed} == completed
    assert {task.task_id for task in revised.tasks if task.completed} == completed_tasks
    assert not any(segment.segment_type == "COLLECTION_STOP" and not segment.completed
                   and segment.target_task_id in completed_tasks for segment in revised.segments)
    blocked = obstacle.polygon.to_shapely_local(ORIGIN)
    for segment in revised.segments:
        if not segment.completed and len(segment.waypoints) > 1:
            path = LineString([p.to_local_meters(ORIGIN) for p in segment.waypoints])
            assert not path.intersects(blocked)
    instance.approve_route(revised.route_id, actor="test", role=OperatorRole.OPERATOR)
    frames = instance.simulator.execute_simulation(revised, machine)
    assert all(task.completed for task in revised.tasks)
    assert all(segment.completed for segment in revised.segments)
    assert instance.state_machine.get_state(revised.job_id) == ExecutionState.COMPLETED
    assert frames[-1].position == revised.disposal_depot
    assert frames[-1].mission_elapsed_min == pytest.approx(revised.estimated_completion_minutes, abs=0.01)
