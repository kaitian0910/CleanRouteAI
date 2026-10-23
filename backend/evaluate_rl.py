"""Run: python -m backend.evaluate_rl --output backend/rl_evaluation.json"""
import argparse
import json
from pathlib import Path
from time import perf_counter

from .orchestrator import AgenticWorkflowOrchestrator
from .rl_scenarios import six_bin_scenario


def run(seeds=(7, 42, 99)):
    rows = []
    for seed in seeds:
        for enabled in (False, True):
            workflow = AgenticWorkflowOrchestrator()
            workflow.routing_engine._force_offline_for_test = True
            req = six_bin_scenario(seed, enabled)
            started = perf_counter()
            route = workflow.plan_and_optimize(req)
            rows.append({
                "seed": seed, "rl_requested": enabled, "policy": route.rl_policy_status.model_dump(),
                "passed": route.kpi_evaluation.passed,
                "metrics": {key: value.actual for key, value in route.kpi_evaluation.metrics.items()},
                "selected_source": next((record.source for record in route.rl_feedback if record.route_id == route.route_id), None),
                "candidate_count": len(route.rl_feedback),
                "rejected_infeasible_candidates": sum(not record.feasible for record in route.rl_feedback),
                "reward": next(record.reward for record in route.rl_feedback if record.route_id == route.route_id),
                "inference_ms": sum(record.inference_ms for record in route.rl_feedback),
                "planning_ms": round((perf_counter()-started)*1000, 2),
            })
    return {"evaluation": "paired-six-bin-simulation-v1", "training": False,
            "note": "Fixed simulated scenarios; not held-out G3 training evaluation or evidence of learning. Both modes use identical planner, routing fallback and KPI gates.",
            "rows": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for row in result["rows"]:
        print(row["seed"], "RL" if row["rl_requested"] else "baseline", row["selected_source"],
              "pass=", row["passed"], "distance=", row["metrics"]["total_distance_m"],
              "duration=", row["metrics"]["estimated_duration_minutes"], "reward=", row["reward"])
