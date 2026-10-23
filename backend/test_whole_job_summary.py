"""Whole-job service totals retain completed depot visits when work is replanned."""
import pytest

from .config import KPIPolicyConfig, settings
from .depot_cycles import plan_depot_cycles
from .evaluator import replay_depot_cycles
from .models import EnvironmentMapData, OperatorRole, PlanRequest
from .test_depot_mission import point, polygon, workflow


def _assert_totals(route):
    services = [segment for segment in route.segments if segment.segment_type == "DEPOT_RECHARGE"]
    outings = {segment.outing_index for segment in route.segments
        if segment.segment_type != "DEPOT_RECHARGE"
        and (segment.distance_m > 1e-7 or segment.estimated_duration_sec > 1e-7)}
    assert route.depot_visits == len(services)
    assert route.charging_duration_minutes == pytest.approx(sum(segment.estimated_duration_sec / 60 for segment in services))
    assert route.outing_count == len(outings)


def _replanned_after_service(monkeypatch):
    monkeypatch.setattr(settings.optimization, "max_iterations", 1)
    instance = workflow()
    machine = instance.machines["sweeper-alpha-01"]
    machine.battery_capacity_kwh = 0.4
    request = PlanRequest(job_id="whole-job-totals", use_rl=False, enable_depot_cycles=True,
        operating_boundary=polygon([(0, 0), (45, 0), (45, 40), (0, 40)]),
        start_location=point(3, 3), disposal_location=point(42, 3),
        environment_map=EnvironmentMapData(map_id="whole-job-clear-test"))
    first = instance.plan_and_optimize(request)
    assert first.kpi_evaluation.passed
    instance.approve_route(first.route_id, "test", OperatorRole.OPERATOR)
    first_service = next(index for index, segment in enumerate(first.segments)
        if segment.segment_type == "DEPOT_RECHARGE")
    instance.simulator.execute_simulation(first, machine, stop_after_segments=first_service + 1)
    request.preserve_completed = True
    replanned = instance.plan_and_optimize(request)
    assert replanned.kpi_evaluation.passed
    return instance, machine, first, replanned


def test_replan_includes_completed_and_pending_services_in_whole_job_summary(monkeypatch):
    instance, machine, first, route = _replanned_after_service(monkeypatch)
    completed = [segment for segment in route.segments
        if segment.completed and segment.segment_type == "DEPOT_RECHARGE"]
    pending = [segment for segment in route.segments
        if not segment.completed and segment.segment_type == "DEPOT_RECHARGE"]
    assert len(completed) == 1 and pending
    _assert_totals(route)
    assert route.depot_visits == len(completed) + len(pending)
    assert route.outing_count == route.depot_visits + 1
    # Safety replay remains about upcoming consumption; historical services
    # count against the lifetime cap without being charged twice in the future.
    policy = KPIPolicyConfig.model_validate(route.kpi_policy_snapshot)
    replay = replay_depot_cycles(route, machine, policy)
    assert not replay["issues"]
    assert replay["depot_visits"] == len(pending)
    assert route.charging_duration_minutes == pytest.approx(
        replay["charging_duration_minutes"] + sum(segment.estimated_duration_sec / 60 for segment in completed))
    assert route.estimated_completion_minutes == pytest.approx(
        route.mission_elapsed_min + replay["duration_minutes"], abs=0.02)
    assert instance.routes_by_id[first.route_id].status.value == "SUPERSEDED"


def test_completed_work_keeps_historical_outings_and_service_totals(monkeypatch):
    instance, machine, _, route = _replanned_after_service(monkeypatch)
    instance.approve_route(route.route_id, "test", OperatorRole.OPERATOR)
    frames = instance.simulator.execute_simulation(route, machine)
    assert all(segment.completed for segment in route.segments)
    historical = route.model_copy(deep=True)
    historical.mission_elapsed_min = frames[-1].mission_elapsed_min
    before = (historical.outing_count, historical.depot_visits, historical.charging_duration_minutes)
    plan_depot_cycles(historical, machine,
        KPIPolicyConfig.model_validate(historical.kpi_policy_snapshot), lambda a, b: [a, b])
    _assert_totals(historical)
    assert before == (historical.outing_count, historical.depot_visits, historical.charging_duration_minutes)
    assert historical.estimated_duration_minutes == 0
    assert historical.estimated_completion_minutes == historical.mission_elapsed_min
