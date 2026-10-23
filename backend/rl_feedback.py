"""Typed, reproducible evaluator feedback; rewards never override feasibility."""
from .config import KPIPolicyConfig, RLRewardConfig, settings
from .models import CandidateFeedback, RouteCandidate


def evaluate_feedback(candidate: RouteCandidate, *, source="DETERMINISTIC_BASELINE",
                      recommendation=None, baseline=None, weights: RLRewardConfig | None = None):
    evaluation = candidate.kpi_evaluation
    if evaluation is None:
        raise ValueError("Candidate must be independently evaluated before reward calculation.")
    weights = weights or settings.rl_reward
    metrics = {key: value.actual for key, value in evaluation.metrics.items()}
    policy = KPIPolicyConfig.model_validate(candidate.kpi_policy_snapshot or {})
    # A multi-outing job can legitimately take hours. Capping duration at two
    # outings would give a three-hour and an eight-hour job identical feedback.
    distance_ratio = metrics.get("total_distance_m", 0.0) / policy.max_travel_distance_m
    duration_ratio = metrics.get("estimated_completion_minutes", metrics.get("estimated_duration_minutes", 0.0)) / policy.max_estimated_duration_minutes
    if not candidate.depot_cycles_enabled:
        distance_ratio = min(2.0, distance_ratio)
        duration_ratio = min(2.0, metrics.get("estimated_duration_minutes", 0.0) / policy.max_estimated_duration_minutes)
    terms = {
        "coverage": weights.coverage * metrics.get("coverage_percent", 100.0) / 100.0,
        "assignment": weights.assignment * metrics.get("planned_collection_assignment_percent", 0.0) / 100.0,
        "battery": weights.battery * metrics.get("remaining_energy_reserve_percent", 0.0) / 100.0,
        "distance": -weights.distance * distance_ratio,
        "duration": -weights.duration * duration_ratio,
        "repeated": -weights.repeated * metrics.get("repeated_coverage_percent", 0.0) / 100.0,
        "hard_failures": -weights.hard_failure * len(evaluation.failed_constraints),
    }
    feasible = evaluation.passed and not candidate.route_safety_issues and not candidate.unreachable_sections
    recommendation = recommendation or {}
    old = baseline.metrics if baseline else {}
    return CandidateFeedback(
        route_id=candidate.route_id, route_version=candidate.route_version, source=source,
        observation=recommendation.get("observation", []),
        proposed_action=recommendation.get("proposed_action"),
        inference_ms=recommendation.get("inference_ms", 0.0),
        reward=round(sum(terms.values()), 6),
        reward_components={key: round(value, 6) for key, value in terms.items()},
        feasible=feasible, failed_constraints=evaluation.failed_constraints,
        decision="ELIGIBLE" if feasible else "REJECTED_HARD_CONSTRAINT",
        reason="Independent KPI and geometry gates passed." if feasible else "; ".join(evaluation.failed_constraints + candidate.route_safety_issues),
        metrics=metrics,
        metric_deltas={key: round(value-old[key], 6) for key, value in metrics.items() if key in old},
    )
