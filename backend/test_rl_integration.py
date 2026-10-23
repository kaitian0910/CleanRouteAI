"""Frozen-policy proposals are evaluated, never treated as authoritative routes."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.config import RLRewardConfig, settings
from backend.models import OperatorRole, OperationalEventRequest
from backend.orchestrator import AgenticWorkflowOrchestrator
from backend.rl_feedback import evaluate_feedback
from backend.rl_scenarios import six_bin_scenario


class StubPolicy:
    def __init__(self, action=None):
        self.action = action

    def status(self):
        return {"mode": "TRAINED_PPO", "checkpoint_loaded": True, "detail": "Test policy, frozen inference"}

    def recommend(self, req, machine, completed_segments=None):
        return {"status": self.status(), "observation": [0.0]*48,
                "proposed_action": self.action or {"action_index": 10, "route_mode": "DIRECT", "preferred_task_id": req.tasks[-1].task_id, "task_id": req.tasks[-1].task_id},
                "inference_ms": 1.0, "reason": "Test dispatch proposal"}


def workflow():
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    instance.rl_policy = StubPolicy()
    return instance


def test_policy_candidate_runs_through_planner_evaluator_and_baseline_selection():
    instance = workflow()
    route = instance.plan_and_optimize(six_bin_scenario())
    assert len(route.rl_feedback) == 2
    baseline, proposal = route.rl_feedback
    assert [baseline.source, proposal.source] == ["DETERMINISTIC_BASELINE", "TRAINED_PPO"]
    assert len(proposal.observation) == 48
    assert proposal.proposed_action["preferred_task_id"] == "sim-bin-6"
    assert proposal.metric_deltas["total_distance_m"] == pytest.approx(proposal.metrics["total_distance_m"]-baseline.metrics["total_distance_m"])
    assert route.kpi_evaluation.passed
    assert route.approved_by is None
    assert instance._latest_route_by_job[route.job_id] == route.route_id
    assert sum(record.decision == "SELECTED_AWAITING_APPROVAL" for record in route.rl_feedback) == 1
    instance.approve_route(route.route_id, actor="test-operator", role=OperatorRole.OPERATOR)
    rejected = next(item for item in instance.job_route_history[route.job_id] if item.route_id != route.route_id)
    with pytest.raises(Exception, match="stale|superseded|latest"):
        instance.approve_route(rejected.route_id, actor="test-operator", role=OperatorRole.OPERATOR)


def test_positive_reward_cannot_select_a_hard_constraint_failure(monkeypatch):
    instance = workflow()
    original = instance.evaluator.evaluate
    calls = 0
    def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            result.passed = False
            result.failed_constraints = ["forced_geometry_failure"]
            result.score = 99999
        return result
    monkeypatch.setattr(instance.evaluator, "evaluate", evaluate)
    monkeypatch.setattr(settings, "rl_reward", RLRewardConfig(hard_failure=0))
    route = instance.plan_and_optimize(six_bin_scenario())
    proposal = route.rl_feedback[1]
    assert proposal.reward > 0 and not proposal.feasible
    assert proposal.decision == "REJECTED_HARD_CONSTRAINT"
    assert route.route_id == route.rl_feedback[0].route_id
    assert route.kpi_evaluation.passed


def test_invalid_policy_action_falls_back_and_attempt_limit_is_respected(monkeypatch):
    instance = workflow()
    instance.rl_policy = StubPolicy({"action_index": 999, "route_mode": "DIRECT", "preferred_task_id": "sim-bin-1"})
    route = instance.plan_and_optimize(six_bin_scenario())
    assert route.rl_policy_status.mode == "DETERMINISTIC_FALLBACK"
    assert len(route.rl_feedback) == 1
    monkeypatch.setattr(settings.optimization, "max_iterations", 1)
    limited = workflow().plan_and_optimize(six_bin_scenario())
    assert len(limited.rl_feedback) == 1


def test_reward_direction_uses_evaluator_values():
    route = workflow().plan_and_optimize(six_bin_scenario())
    old = evaluate_feedback(route)
    better = route.model_copy(deep=True)
    better.kpi_evaluation.metrics["total_distance_m"].actual *= 0.5
    better.kpi_evaluation.metrics["estimated_duration_minutes"].actual *= 0.5
    record = evaluate_feedback(better, baseline=old)
    assert record.reward > old.reward
    assert record.metric_deltas["total_distance_m"] < 0
    assert record.reward == pytest.approx(sum(record.reward_components.values()), abs=1e-5)


def test_feedback_api_uses_current_simulator_and_audit_state(monkeypatch):
    instance = workflow()
    monkeypatch.setattr(main, "orchestrator", instance)
    client = TestClient(main.app)
    client.headers["Authorization"] = f"Bearer {settings.operator_token}"
    route = instance.plan_and_optimize(six_bin_scenario())
    path = f"/api/routes/{route.route_id}"
    before = client.get(path+"/feedback").json()
    assert before["execution_feedback"] == []
    assert len(before["candidates"]) == 2
    assert client.post(path+"/approve").status_code == 200
    assert client.post(path+"/execute", json={"stop_after_segments": 1}).status_code == 200
    after = client.get(path+"/feedback").json()
    assert after["execution_feedback"][0]["completed_segments"] == 1
    assert after["execution_feedback"][0]["mission_elapsed_min"] > 0
    log = client.get(path+"/metrics").json()["audit_log"]
    assert any(item["action"] == "PLAN_SELECTED" for item in log)
    technical = client.get(path+"/metrics").json()["technical_audit_log"]
    assert any(item["action"] == "CANDIDATE_EVALUATED" for item in technical)
    assert client.get("/api/rl/status").status_code == 200
    assert client.get("/api/routes/missing/feedback").status_code == 404


def test_policy_exception_does_not_break_baseline():
    instance = workflow()
    def fail(*args, **kwargs):
        raise RuntimeError("Unavailable")
    instance.rl_policy.recommend = fail
    route = instance.plan_and_optimize(six_bin_scenario())
    assert route.kpi_evaluation.passed
    assert route.rl_policy_status.mode == "DETERMINISTIC_FALLBACK"


def test_timeout_does_not_start_another_policy_candidate(monkeypatch):
    import backend.orchestrator as module
    times = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(module, "monotonic", lambda: next(times, 100.0))
    route = workflow().plan_and_optimize(six_bin_scenario())
    assert len(route.rl_feedback) == 1
    assert route.kpi_evaluation.passed


def test_changed_window_feedback_replans_once_from_recorded_execution():
    instance = workflow()
    route = instance.plan_and_optimize(six_bin_scenario())
    instance.approve_route(route.route_id, actor="operator", role=OperatorRole.OPERATOR)
    machine = instance.machines[route.machine_id]
    instance.simulator.execute_simulation(route, machine, stop_after_segments=2)
    frame = instance.simulator.telemetry_store[route.route_id][-1]
    task = next(task for task in route.tasks if not task.completed)
    event = OperationalEventRequest(job_id=route.job_id, route_id=route.route_id,
                                    event_type="TIME_WINDOW_CHANGED", task_id=task.task_id,
                                    task_time_window_start_min=0, task_time_window_end_min=470, idempotency_key="rl-window-feedback")
    result = instance.handle_operational_event(event, actor="operator", role=OperatorRole.OPERATOR)
    revised = result["candidate"]
    assert revised.origin == frame.position
    assert revised.mission_elapsed_min == frame.mission_elapsed_min
    assert any(record.source == "TRAINED_PPO" for record in revised.rl_feedback)
    history_count = len(instance.job_route_history[route.job_id])
    instance.handle_operational_event(event, actor="operator", role=OperatorRole.OPERATOR)
    assert len(instance.job_route_history[route.job_id]) == history_count
