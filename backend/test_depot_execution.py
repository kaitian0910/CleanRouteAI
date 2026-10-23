"""Battery-service missions are validated from legs, not claimed summaries."""
import pytest

from backend.config import KPIPolicyConfig
from backend.evaluator import KPIRouteQualityEvaluator
from backend.depot_cycles import plan_depot_cycles
from backend.execution import DeterministicRouteSimulator, ExecutionStateMachine, SafetyInterlockError
from backend.models import (
    AuditAndEventManager, Coordinate, ExecutionState, MachineProfile,
    RouteCandidate, RouteLifecycleStatus, RouteSegment, RubbishItemSpec, TaskItem, TaskType,
)


def mission():
    depot = Coordinate(lat=3.14, lon=101.68)
    work = Coordinate(lat=3.1409, lon=101.68)
    machine = MachineProfile(machine_id="cycles", name="test", battery_capacity_kwh=1,
        current_battery_percent=65, energy_consumption_kwh_per_km_travel=2,
        depot_service_minutes=5)
    segments = []
    for index in range(3):
        segments.extend([
            RouteSegment(segment_type="TRANSIT", waypoints=[depot, work], distance_m=100,
                         estimated_duration_sec=180),
            RouteSegment(segment_type="RETURN_TO_BASE", waypoints=[work, depot], distance_m=100,
                         estimated_duration_sec=180),
        ])
        if index < 2:
            segments.append(RouteSegment(segment_type="DEPOT_RECHARGE", waypoints=[depot],
                distance_m=0, estimated_duration_sec=300))
    route = RouteCandidate(job_id="service", machine_id=machine.machine_id,
        route_version=1, task_mode=TaskType.RUBBISH_COLLECTION, origin=depot,
        disposal_depot=depot, depot_cycles_enabled=True, segments=segments,
        # Deliberately dishonest forecast summaries must not bypass replay.
        estimated_remaining_battery_percent=100, total_distance_m=0,
        kpi_policy_snapshot=KPIPolicyConfig(max_estimated_duration_minutes=7,
            max_travel_distance_m=250).model_dump())
    return route, machine, KPIPolicyConfig.model_validate(route.kpi_policy_snapshot)


def approved_simulator(route, machine, policy):
    route.kpi_evaluation = KPIRouteQualityEvaluator().evaluate(route, machine, policy)
    assert route.kpi_evaluation.passed, route.kpi_evaluation.failed_constraints
    route.status = RouteLifecycleStatus.APPROVED
    route.approved_by = "operator"
    audit = AuditAndEventManager()
    state = ExecutionStateMachine(audit)
    state._states[route.job_id] = ExecutionState.READY
    return DeterministicRouteSimulator(state, audit)


def test_multi_outing_exceeds_total_limits_but_completes_after_timed_services():
    route, machine, policy = mission()
    simulator = approved_simulator(route, machine, policy)
    metrics = route.kpi_evaluation.metrics
    assert metrics["total_distance_m"].actual > policy.max_travel_distance_m
    assert not metrics["total_distance_m"].is_constraint
    assert metrics["estimated_duration_minutes"].actual == pytest.approx(28)
    assert metrics["outing_count"].actual == 3
    frames = simulator.execute_simulation(route, machine)
    assert route.status == RouteLifecycleStatus.COMPLETED
    services = [frame for frame in frames if frame.speed_mps == 0]
    assert len(services) == 2
    assert all(frame.battery_percent == 100 for frame in services)
    assert frames[-1].mission_elapsed_min == pytest.approx(28)
    assert all(frame.battery_percent >= policy.min_energy_reserve_percent for frame in frames)
    assert sum(entry.action == "DEPOT_RECHARGE" for entry in simulator.audit._audit_log) == 2


@pytest.mark.parametrize("tampering", ["short", "away", "teleport", "missing_charge", "understated_distance"])
def test_replay_rejects_unsafe_recharge_and_battery_claims(tampering):
    route, machine, policy = mission()
    if tampering == "short":
        route.segments[2].estimated_duration_sec = 1
    elif tampering == "away":
        route.segments[2].waypoints = [route.segments[0].waypoints[-1]]
    elif tampering == "teleport":
        route.segments[1].waypoints[-1] = route.segments[0].waypoints[-1]
    elif tampering == "missing_charge":
        route.segments = [seg for seg in route.segments if seg.segment_type != "DEPOT_RECHARGE"]
    else:
        for seg in route.segments:
            seg.distance_m = 0
        machine.current_battery_percent = 25
    result = KPIRouteQualityEvaluator().evaluate(route, machine, policy)
    assert not result.passed


def test_live_battery_drift_rejected_before_motion_despite_approved_forecast():
    route, machine, policy = mission()
    simulator = approved_simulator(route, machine, policy)
    machine.current_battery_percent = 25
    with pytest.raises(SafetyInterlockError, match="depot-cycle"):
        simulator.execute_simulation(route, machine)
    assert not any(seg.completed for seg in route.segments)


def test_pause_resume_uses_live_battery_and_preserves_elapsed_service_time():
    route, machine, policy = mission()
    simulator = approved_simulator(route, machine, policy)
    simulator.execute_simulation(route, machine, stop_after_segments=3)
    assert machine.current_battery_percent == 100
    assert simulator.state_machine.get_state(route.job_id) == ExecutionState.PAUSED
    simulator.state_machine.transition(route.job_id, ExecutionState.EXECUTING, "operator", "resume")
    frames = simulator.execute_simulation(route, machine)
    assert frames[-1].mission_elapsed_min == pytest.approx(28)
    assert len(frames) == len(route.segments)


def test_snapshot_battery_reserve_enforced_over_global_default():
    route, machine, policy = mission()
    policy.min_energy_reserve_percent = 40
    route.kpi_policy_snapshot = policy.model_dump()
    result = KPIRouteQualityEvaluator().evaluate(route, machine, policy)
    assert "energy_reserve_below_minimum" in result.failed_constraints


def test_completed_prefix_counts_against_current_outing_but_not_future_finish_time():
    route, machine, policy = mission()
    simulator = approved_simulator(route, machine, policy)
    simulator.execute_simulation(route, machine, stop_after_segments=1)
    policy.max_estimated_duration_minutes = 5
    result = KPIRouteQualityEvaluator().evaluate(route, machine, policy,
        simulator.telemetry_store[route.route_id])
    assert "duration_exceeds_target_max" in result.failed_constraints
    assert result.metrics["max_outing_duration_minutes"].actual == pytest.approx(6)
    assert result.metrics["estimated_completion_minutes"].actual == pytest.approx(28)


def test_initial_low_battery_can_receive_service_when_already_at_depot():
    route, machine, policy = mission()
    machine.current_battery_percent = 10
    route.segments.insert(0, RouteSegment(segment_type="DEPOT_RECHARGE",
        waypoints=[route.disposal_depot], distance_m=0, estimated_duration_sec=300))
    simulator = approved_simulator(route, machine, policy)
    frames = simulator.execute_simulation(route, machine)
    assert frames[0].battery_percent == 100
    assert frames[-1].mission_elapsed_min == pytest.approx(33)


def test_replay_enforces_service_cap_including_completed_prefix():
    route, machine, policy = mission()
    policy.max_depot_cycles = 1
    route.segments[2].completed = True
    result = KPIRouteQualityEvaluator().evaluate(route, machine, policy)
    assert "depot_cycle_service_limit_exceeded" in result.failed_constraints


def collection_after_initial_recharge(window_start, window_end):
    depot = Coordinate(lat=3.14, lon=101.68)
    bin_location = Coordinate.from_local_meters(100, 0, depot)
    machine = MachineProfile(machine_id="windows", name="test", current_battery_percent=10,
        current_payload_kg=30, current_payload_volume_m3=0.1)
    task = TaskItem(task_type=TaskType.RUBBISH_COLLECTION, location=bin_location,
        service_duration_sec=60, time_window_start_min=window_start, time_window_end_min=window_end,
        rubbish_spec=RubbishItemSpec(estimated_weight_kg=5, estimated_volume_m3=0.05))
    route = RouteCandidate(job_id="hybrid-windows", machine_id=machine.machine_id, route_version=1,
        task_mode=TaskType.HYBRID, origin=depot, disposal_depot=depot,
        target_area_m2=1, covered_area_m2=1, tasks=[task], segments=[
            RouteSegment(segment_type="TRANSIT", waypoints=[depot, bin_location], distance_m=100,
                estimated_duration_sec=100 / machine.cruise_speed_mps),
            RouteSegment(segment_type="COLLECTION_STOP", waypoints=[bin_location], distance_m=0,
                estimated_duration_sec=60, target_task_id=task.task_id),
            RouteSegment(segment_type="RETURN_TO_BASE", waypoints=[bin_location, depot], distance_m=100,
                estimated_duration_sec=100 / machine.cruise_speed_mps),
        ])
    policy = KPIPolicyConfig()
    route.kpi_policy_snapshot = policy.model_dump()
    plan_depot_cycles(route, machine, policy, lambda a, b: [a, b])
    return route, machine, policy


def test_hybrid_recharge_time_counts_toward_collection_deadline():
    route, machine, policy = collection_after_initial_recharge(5, 15)
    assert route.segments[0].segment_type == "DEPOT_RECHARGE"
    assert route.task_schedule[0].service_start_min > 20
    assert route.task_schedule[0].status == "INFEASIBLE"
    assert not any(segment.segment_type == "WAIT_FOR_WINDOW" for segment in route.segments)
    result = KPIRouteQualityEvaluator().evaluate(route, machine, policy)
    assert "task_time_window_infeasible" in result.failed_constraints
    assert not result.passed


def test_hybrid_wait_is_shortened_by_recharge_and_collection_then_unloads():
    route, machine, policy = collection_after_initial_recharge(25, 30)
    schedule = route.task_schedule[0]
    assert schedule.service_start_min == pytest.approx(25)
    assert schedule.service_end_min == pytest.approx(26)
    assert 3 < schedule.waiting_min < 4
    simulator = approved_simulator(route, machine, policy)
    frames = simulator.execute_simulation(route, machine)
    assert frames[0].payload_kg == 0  # Starting depot service unloads existing waste.
    collection_frame = next(frame for frame, segment in zip(frames, route.segments)
        if segment.segment_type == "COLLECTION_STOP")
    assert collection_frame.mission_elapsed_min == pytest.approx(26)
    assert collection_frame.payload_kg == 5
    assert route.tasks[0].completed
    assert frames[-1].payload_kg == 0
    assert machine.current_payload_volume_m3 == 0
    assert sum(segment.segment_type == "DEPOT_RECHARGE" for segment in route.segments) == 1
    assert frames[-1].mission_elapsed_min == pytest.approx(route.estimated_completion_minutes, abs=0.002)
