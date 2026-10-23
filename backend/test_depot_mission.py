"""End-to-end depot missions using the production planner and simulator."""
import pytest
from shapely.geometry import LineString

from backend.models import (
    Coordinate, EnvironmentMapData, ExecutionState, ExclusionZone,
    GeoPolygon, OperatorRole, PlanRequest,
)
from backend.orchestrator import AgenticWorkflowOrchestrator


ORIGIN = Coordinate(lat=3.139, lon=101.6869)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


def workflow():
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    return instance


def test_large_mission_returns_recharges_resumes_and_finishes_all_work():
    instance = workflow()
    request = PlanRequest(
        job_id="large-depot-mission", enable_depot_cycles=True, use_rl=False,
        operating_boundary=polygon([(0, 0), (115, 0), (115, 155), (0, 155)]),
        start_location=point(3, 3), disposal_location=point(110, 3),
        environment_map=EnvironmentMapData(map_id="verified-clear-test-terrain"),
    )
    route = instance.plan_and_optimize(request, actor="test")
    assert route.kpi_evaluation.passed, route.kpi_evaluation.failed_constraints
    assert not route.route_safety_issues
    assert route.estimated_duration_minutes > 90
    assert route.outing_count == 3 and route.depot_visits == 2
    assert route.max_outing_duration_minutes <= 90
    machine = instance.machines[route.machine_id]
    service_index = next(i for i, s in enumerate(route.segments) if s.segment_type == "DEPOT_RECHARGE")
    instance.approve_route(route.route_id, actor="test", role=OperatorRole.OPERATOR)
    frames = instance.simulator.execute_simulation(route, machine, stop_after_segments=service_index + 1)
    before, after = frames[-2:]
    assert before.position == route.disposal_depot == after.position
    assert before.battery_percent < after.battery_percent == machine.recharged_battery_percent
    assert after.mission_elapsed_min - before.mission_elapsed_min == pytest.approx(machine.depot_service_minutes, abs=0.001)
    assert machine.current_battery_percent == machine.recharged_battery_percent
    assert any(not s.completed and s.segment_type == "SWEEP_SWATH" for s in route.segments)

    assert instance.state_machine.get_state(route.job_id) == ExecutionState.PAUSED
    instance.state_machine.transition(route.job_id, ExecutionState.EXECUTING, "test", "Resume after depot service",
                                      role=OperatorRole.OPERATOR, mandatory_constraints_passed=True)
    frames = instance.simulator.execute_simulation(route, machine)
    assert instance.state_machine.get_state(route.job_id) == ExecutionState.COMPLETED
    assert all(s.completed for s in route.segments)
    assert frames[-1].position == route.disposal_depot
    assert frames[-1].mission_elapsed_min == pytest.approx(route.estimated_completion_minutes, abs=0.01)
    assert min(f.battery_percent for f in frames) >= 20
    assert sum(s.segment_type == "DEPOT_RECHARGE" and s.completed for s in route.segments) == 2


def test_live_obstacle_replan_keeps_completed_work_and_finishes_with_depot_services():
    instance = workflow()
    machine = instance.machines["sweeper-alpha-01"]
    machine.battery_capacity_kwh = 0.6  # Force service stops on this compact test site.
    request = PlanRequest(
        job_id="obstacle-depot-mission", enable_depot_cycles=True, use_rl=False,
        operating_boundary=polygon([(0, 0), (45, 0), (45, 40), (0, 40)]),
        start_location=point(3, 3), disposal_location=point(42, 3),
        environment_map=EnvironmentMapData(map_id="verified-clear-test-terrain"),
    )
    first = instance.plan_and_optimize(request, actor="test")
    assert first.kpi_evaluation.passed, first.kpi_evaluation.failed_constraints
    instance.approve_route(first.route_id, actor="test", role=OperatorRole.OPERATOR)
    frames = instance.simulator.execute_simulation(first, machine, stop_after_segments=2)
    measured = frames[-1]
    completed = {s.segment_id: s.model_dump() for s in first.segments if s.completed}
    battery = machine.current_battery_percent
    request.preserve_completed = True
    obstacle = ExclusionZone(name="New obstacle", polygon=polygon([(19, 16), (27, 16), (27, 24), (19, 24)]))
    request.exclusion_zones = [obstacle]
    revised = instance.plan_and_optimize(request, actor="test")
    assert revised.kpi_evaluation.passed, (revised.kpi_evaluation.failed_constraints, revised.route_safety_issues)
    assert revised.depot_visits > 0
    assert revised.origin == measured.position
    assert revised.mission_elapsed_min == measured.mission_elapsed_min
    assert machine.current_battery_percent == battery
    assert revised.route_version > first.route_version
    assert {s.segment_id: s.model_dump() for s in revised.segments if s.completed} == completed
    blocked = obstacle.polygon.to_shapely_local(ORIGIN)
    for segment in revised.segments:
        if not segment.completed and len(segment.waypoints) > 1:
            line = LineString([p.to_local_meters(ORIGIN) for p in segment.waypoints])
            assert not line.intersects(blocked)
    instance.approve_route(revised.route_id, actor="test", role=OperatorRole.OPERATOR)
    finished = instance.simulator.execute_simulation(revised, machine)
    assert all(s.completed for s in revised.segments)
    assert instance.state_machine.get_state(revised.job_id) == ExecutionState.COMPLETED
    assert finished[-1].position == revised.disposal_depot
    assert finished[-1].mission_elapsed_min == pytest.approx(revised.estimated_completion_minutes, abs=0.01)
    assert min(f.battery_percent for f in finished) >= 20
