"""Past sweep settings are advisory; each new job still needs fresh evaluation."""
import pytest

from .config import KPIPolicyConfig, settings
from .mission_history import MissionHistory, layout_fingerprint, sweep_context_fingerprint
from .models import MachineProfile, RouteSegment, RubbishItemSpec, TaskItem, TaskScheduleEntry, TaskType
from .orchestrator import AgenticWorkflowOrchestrator
from .repeat_planning import compare_repeat_plan
from .rl_feedback import evaluate_feedback
from .test_mission_history import planned_route, point, workload


def context(request, machine, policy=None):
    return sweep_context_fingerprint(
        request, machine, policy or KPIPolicyConfig(), request.environment_map
    )


def test_sweep_history_context_survives_changed_start_battery_load_and_clock():
    original = workload()
    machine = MachineProfile(machine_id="repeat-context", name="Repeat context")
    revised = original.model_copy(deep=True)
    revised.start_location = point(4, 3)
    revised.mission_elapsed_min = 37
    # Every repeat gives recurring bins a fresh key as well as a fresh ID.
    revised.tasks[0].task_id = "next-round-bin"
    revised.tasks[0].idempotency_key = "next-round-collection-request"
    live_machine = machine.model_copy(update={
        "current_battery_percent": 36,
        "current_payload_kg": 12,
        "current_payload_volume_m3": 0.2,
    })
    assert context(revised, live_machine) == context(original, machine)
    # Comparable performance records keep their stricter resource-state key.
    assert layout_fingerprint(revised, live_machine, KPIPolicyConfig(), revised.environment_map) != (
        layout_fingerprint(original, machine, KPIPolicyConfig(), original.environment_map)
    )


@pytest.mark.parametrize("change", [
    "boundary", "obstacle", "width", "speed", "battery_capacity", "capacity",
    "depot", "window", "weight", "service", "task_type", "policy", "cycles",
    "verified_terrain",
])
def test_sweep_history_context_does_not_cross_changed_operational_constraints(change):
    original = workload()
    original.tasks[0].rubbish_spec = RubbishItemSpec(estimated_weight_kg=15)
    machine = MachineProfile(machine_id="repeat-context", name="Repeat context")
    expected = context(original, machine)
    revised, robot, policy = original.model_copy(deep=True), machine.model_copy(deep=True), KPIPolicyConfig()
    if change == "boundary":
        revised.operating_boundary.vertices[1] = point(32, 0)
    elif change == "obstacle":
        revised.exclusion_zones[0].polygon.vertices[0] = point(19, 5)
    elif change == "width":
        robot.working_width_m = 2
    elif change == "speed":
        robot.sweep_speed_mps = 0.8
    elif change == "battery_capacity":
        robot.battery_capacity_kwh = 6
    elif change == "capacity":
        robot.max_payload_kg = 100
    elif change == "depot":
        revised.disposal_location = point(4, 2)
    elif change == "window":
        revised.tasks[0].time_window_end_min = 100
    elif change == "weight":
        revised.tasks[0].rubbish_spec.estimated_weight_kg = 25
    elif change == "service":
        revised.tasks[0].service_duration_sec = 90
    elif change == "task_type":
        revised.tasks[0].task_type = TaskType.OVERSIZED_RUBBISH
    elif change == "policy":
        policy.max_estimated_duration_minutes = 60
    elif change == "cycles":
        revised.enable_depot_cycles = False
    elif change == "verified_terrain":
        revised.environment_map.require_verified_terrain = True
    assert context(revised, robot, policy) != expected


def remember(history, angle, estimate, recorded=None, exact="past-exact", shared="shared-sweep"):
    route = planned_route(estimated_completion_minutes=estimate, sweep_angle_deg=angle)
    history.record_plan(route, exact, shared)
    if recorded is not None:
        history.record_completion(route, recorded)
    return route


def test_completed_simulation_times_seed_sweep_before_optimistic_unexecuted_estimates():
    history = MissionHistory()
    remember(history, 0, estimate=10, recorded=100)
    remember(history, 90, estimate=50, recorded=40)
    remember(history, 45, estimate=1)  # Never executed: cannot beat observed results.
    assert history.preferred_angle("new-resource-state", "shared-sweep") == 90
    # A structurally different workload supplies no history hint.
    assert history.preferred_angle("new-resource-state", "different-sweep") is None


def test_sweep_history_uses_typical_completed_time_instead_of_single_lucky_run():
    history = MissionHistory()
    remember(history, 0, estimate=10, recorded=20)
    remember(history, 0, estimate=10, recorded=120)
    remember(history, 90, estimate=50, recorded=60)
    assert history.preferred_angle("new-resource-state", "shared-sweep") == 90


def test_unexecuted_sweep_history_is_only_an_estimate_fallback():
    history = MissionHistory()
    remember(history, 0, estimate=55)
    remember(history, 90, estimate=45)
    assert history.preferred_angle("new-resource-state", "shared-sweep") == 90


def test_changed_resource_history_hint_is_regenerated_and_evaluated(monkeypatch):
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    monkeypatch.setattr(settings.optimization, "max_iterations", 2)
    request = workload()
    request.tasks = []
    request.exclusion_zones = []
    first = instance.plan_and_optimize(request, actor="test")
    robot = instance.machines[request.machine_id]
    shared = sweep_context_fingerprint(
        request, robot, KPIPolicyConfig(), first.planning_environment_map, mode=TaskType.AREA_SWEEP
    )
    remembered = remember(instance.mission_history, 90, estimate=0.01, recorded=0.02, shared=shared)
    calls, evaluations = [], []
    make_candidate, evaluate = instance._make_candidate, instance.evaluator.evaluate

    def capture_candidate(*args, **kwargs):
        calls.append(list(args[5]))
        return make_candidate(*args, **kwargs)

    def capture_evaluation(candidate, *args, **kwargs):
        evaluations.append(candidate.route_id)
        return evaluate(candidate, *args, **kwargs)

    monkeypatch.setattr(instance, "_make_candidate", capture_candidate)
    monkeypatch.setattr(instance.evaluator, "evaluate", capture_evaluation)
    robot.current_battery_percent = 36
    revised = request.model_copy(deep=True)
    revised.job_id = "same-site-different-resources"
    revised.start_location = point(4, 3)
    selected = instance.plan_and_optimize(revised, actor="test")
    assert calls[0][0] == 90
    assert selected.route_id not in {first.route_id, remembered.route_id}
    assert selected.route_id in evaluations
    assert selected.kpi_evaluation.passed
    assert selected.initial_battery_percent == 36
    assert selected.origin == revised.start_location
    assert selected.estimated_completion_minutes > 0.02
    assert not selected.rl_policy_status.training_enabled


def route_to_compare():
    tasks = [TaskItem(task_id=f"old-bin-{number}", task_type=TaskType.RUBBISH_COLLECTION,
        location=point(x, 4), completed=True) for number, x in enumerate((3, 8))]
    schedule = [TaskScheduleEntry(task_id=task.task_id, arrival_min=i, service_start_min=i,
        service_end_min=i + 1, waiting_min=0, window_start_min=0, window_end_min=90,
        priority=5) for i, task in enumerate(tasks)]
    return planned_route(estimated_duration_minutes=100, estimated_completion_minutes=100,
        tasks=tasks, task_schedule=schedule, segments=[
            RouteSegment(segment_type="TRANSIT", waypoints=[point(), point(2, 2)],
                distance_m=2.8, estimated_duration_sec=2.8, completed=True),
            RouteSegment(segment_type="SWEEP_SWATH", waypoints=[point(2, 2), point(20, 2)],
                distance_m=18, estimated_duration_sec=18, completed=True),
        ])


def fresh_comparison_route(source):
    repeated = source.model_copy(deep=True)
    repeated.route_id = "new-route"
    repeated.job_id = "new-job"
    repeated.estimated_completion_minutes = 90
    for index, task in enumerate(repeated.tasks):
        task.task_id = f"fresh-bin-{index}"
        task.completed = False
        repeated.task_schedule[index].task_id = task.task_id
    for index, segment in enumerate(repeated.segments):
        segment.segment_id = f"fresh-segment-{index}"
        segment.completed = False
    repeated.rl_feedback = [evaluate_feedback(repeated)]
    return repeated


def test_repeat_comparison_distinguishes_ids_progress_estimates_and_past_recorded_time():
    source = route_to_compare()
    repeated = fresh_comparison_route(source)
    comparison = compare_repeat_plan(source, repeated, previous_elapsed=120)
    assert not comparison.sweep_pattern_changed
    assert not comparison.travel_path_changed
    assert not comparison.collection_order_changed
    assert comparison.candidates_checked == comparison.feasible_candidates == 1
    assert comparison.previous_estimated_completion_minutes == 100
    assert comparison.previous_actual_completion_minutes == 120
    assert comparison.new_estimated_completion_minutes == 90
    assert comparison.estimated_time_change_minutes == -10  # Estimate vs estimate, not 120 vs 90.
    assert "same path" in comparison.summary.lower()


@pytest.mark.parametrize("part", ["travel", "sweep", "collection_order"])
def test_repeat_comparison_identifies_which_physical_part_changed(part):
    source = route_to_compare()
    repeated = fresh_comparison_route(source)
    if part == "travel":
        repeated.segments[0].waypoints.insert(1, point(0, 2))
    elif part == "sweep":
        repeated.sweep_angle_deg = 90
        repeated.segments[1].waypoints = [point(2, 2), point(2, 20)]
    else:
        repeated.task_schedule.reverse()
    comparison = compare_repeat_plan(source, repeated)
    assert comparison.sweep_pattern_changed == (part == "sweep")
    assert comparison.collection_order_changed == (part == "collection_order")
    assert comparison.travel_path_changed == (part in {"travel", "sweep"})
    assert comparison.previous_actual_completion_minutes is None
