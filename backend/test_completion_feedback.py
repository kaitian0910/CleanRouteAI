"""Completion-time feedback must distinguish long jobs and retain feasible plans."""
import pytest

from backend.config import settings
from backend.models import ExecutionState, RouteLifecycleStatus
from backend.rl_feedback import evaluate_feedback
from backend.test_depot_mission import workflow, polygon, point
from backend.models import EnvironmentMapData, PlanRequest


def request():
    return PlanRequest(enable_depot_cycles=True, use_rl=False,
        operating_boundary=polygon([(0, 0), (115, 0), (115, 155), (0, 155)]),
        start_location=point(3, 3), disposal_location=point(110, 3),
        environment_map=EnvironmentMapData(map_id="clear-comparison"))


def test_comparison_selects_shortest_feasible_whole_job_and_publishes_it():
    instance = workflow()
    route = instance.plan_and_optimize(request())
    assert len(route.rl_feedback) > 1
    feasible = [r for r in route.rl_feedback if r.feasible]
    selected = next(r for r in feasible if r.route_id == route.route_id)
    assert selected.metrics["estimated_completion_minutes"] == min(r.metrics["estimated_completion_minutes"] for r in feasible)
    assert route.status == RouteLifecycleStatus.PROPOSED
    assert instance._latest_route_by_job[route.job_id] == route.route_id
    assert instance.state_machine.get_state(route.job_id) == ExecutionState.AWAITING_APPROVAL
    assert sum(r.decision == "SELECTED_AWAITING_APPROVAL" for r in route.rl_feedback) == 1
    assert all(r.status == RouteLifecycleStatus.SUPERSEDED for r in instance.job_route_history[route.job_id] if r.route_id != route.route_id)


def test_long_job_duration_feedback_has_no_two_outing_plateau(monkeypatch):
    monkeypatch.setattr(settings.optimization, "max_iterations", 1)
    route = workflow().plan_and_optimize(request())
    assert route.estimated_completion_minutes > 180
    original = evaluate_feedback(route)
    slower = route.model_copy(deep=True)
    slower.kpi_evaluation.metrics["estimated_completion_minutes"].actual += 90
    feedback = evaluate_feedback(slower)
    assert feedback.reward_components["duration"] == pytest.approx(original.reward_components["duration"] - settings.rl_reward.duration)
    assert feedback.reward < original.reward


def test_failed_later_comparison_cannot_displace_feasible_baseline(monkeypatch):
    instance = workflow()
    original = instance.evaluator.evaluate
    calls = 0
    def evaluate(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls > 1:
            result.passed = False
            result.failed_constraints = ["forced_geometry_failure"]
            result.score = 99999
        return result
    monkeypatch.setattr(instance.evaluator, "evaluate", evaluate)
    route = instance.plan_and_optimize(request())
    assert calls > 1 and route.kpi_evaluation.passed
    assert route.route_id == route.rl_feedback[0].route_id
    assert route.status == RouteLifecycleStatus.PROPOSED
    assert instance.state_machine.get_state(route.job_id) == ExecutionState.AWAITING_APPROVAL


def test_optional_later_candidate_exception_retains_approvable_baseline(monkeypatch):
    instance = workflow()
    original = instance._make_candidate
    calls = 0
    def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("Transient optional comparison failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(instance, "_make_candidate", generate)
    route = instance.plan_and_optimize(request())
    assert calls > 1
    assert route.kpi_evaluation.passed
    assert route.route_id == route.rl_feedback[0].route_id
    assert route.status == RouteLifecycleStatus.PROPOSED
    assert instance._latest_route_by_job[route.job_id] == route.route_id
    assert instance.state_machine.get_state(route.job_id) == ExecutionState.AWAITING_APPROVAL
    assert len(instance.mission_history.records()) == 1
