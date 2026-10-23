from math import hypot
from typing import Any, Dict, List, Optional
from .config import KPIPolicyConfig
from .models import (
    DataProvenance,
    KPIEvaluationResponse,
    MachineProfile,
    MetricEntry,
    RouteCandidate,
    TaskType,
    TelemetryFrame,
)


def replay_depot_cycles(
    candidate: RouteCandidate, machine: MachineProfile, policy: KPIPolicyConfig,
    mission_elapsed_min: Optional[float] = None,
) -> Dict[str, Any]:
    """Independently replay uncompleted legs; planner summary fields are not trusted."""
    battery = machine.current_battery_percent
    minimum = 100.0
    position = candidate.origin
    elapsed = distance = total_minutes = total_distance = service_minutes = 0.0
    max_minutes = max_distance = 0.0
    visits = outings = 0
    completed_visits = sum(segment.completed and segment.segment_type == "DEPOT_RECHARGE" for segment in candidate.segments)
    issues: List[str] = []
    active = False
    clock_start = candidate.mission_elapsed_min if mission_elapsed_min is None else mission_elapsed_min
    schedules = {entry.task_id: entry for entry in candidate.task_schedule}
    for segment in candidate.segments:
        if segment.completed:
            if segment.waypoints:
                position = segment.waypoints[-1]
            if segment.segment_type == "DEPOT_RECHARGE":
                elapsed = distance = 0.0
                active = False
            else:
                geometry_distance = sum(hypot(*b.to_local_meters(a))
                    for a, b in zip(segment.waypoints, segment.waypoints[1:]))
                previous_distance = max(segment.distance_m, geometry_distance, 0.0)
                speed = machine.sweep_speed_mps if segment.segment_type in {"SWEEP_SWATH", "TURN_CONNECTOR"} else machine.cruise_speed_mps
                distance += previous_distance
                elapsed += max(segment.estimated_duration_sec / 60.0, previous_distance / speed / 60.0)
                active = True
            continue
        if not segment.waypoints:
            issues.append("depot_cycle_missing_geometry")
            continue
        first, last = segment.waypoints[0], segment.waypoints[-1]
        if segment.segment_type == "RETURN_TO_BASE" and hypot(*last.to_local_meters(candidate.disposal_depot)) > 1.0:
            issues.append("invalid_depot_return")
        if hypot(*first.to_local_meters(position)) > 2.0:
            issues.append("depot_cycle_discontinuous_route")
        geometric_distance = sum(
            hypot(*b.to_local_meters(a))
            for a, b in zip(segment.waypoints, segment.waypoints[1:])
        )
        leg_distance = max(0.0, segment.distance_m, geometric_distance)
        minutes = max(0.0, segment.estimated_duration_sec) / 60.0
        if segment.segment_type == "DEPOT_RECHARGE":
            valid = (
                all(hypot(*point.to_local_meters(candidate.disposal_depot)) <= 1.0
                    for point in segment.waypoints)
                and hypot(*position.to_local_meters(candidate.disposal_depot)) <= 1.0
                and leg_distance <= 0.1
                and minutes + 1e-6 >= machine.depot_service_minutes
            )
            if not valid:
                issues.append("invalid_depot_recharge")
            else:
                max_minutes, max_distance = max(max_minutes, elapsed), max(max_distance, distance)
                if active:
                    outings += 1
                visits += 1
                service_minutes += minutes
                elapsed = distance = 0.0
                active = False
                battery = machine.recharged_battery_percent
                minimum = min(minimum, battery)
            total_minutes += minutes
            position = last
            continue
        speed = machine.sweep_speed_mps if segment.segment_type in {"SWEEP_SWATH", "TURN_CONNECTOR"} else machine.cruise_speed_mps
        minutes = max(minutes, leg_distance / speed / 60.0)
        if segment.segment_type == "COLLECTION_STOP" and segment.target_task_id:
            schedule = schedules.get(segment.target_task_id)
            if (schedule is None or clock_start + total_minutes + 0.02 < schedule.window_start_min
                or clock_start + total_minutes + minutes > schedule.window_end_min + 0.02):
                issues.append("task_time_window_infeasible")
        rate = machine.energy_consumption_kwh_per_km_sweep if segment.segment_type == "SWEEP_SWATH" else machine.energy_consumption_kwh_per_km_travel
        battery -= leg_distance / 1000.0 * rate / machine.battery_capacity_kwh * 100.0
        minimum = min(minimum, battery)
        if battery + 1e-6 < policy.min_energy_reserve_percent:
            issues.append("energy_reserve_below_minimum")
        elapsed += minutes
        distance += leg_distance
        total_minutes += minutes
        total_distance += leg_distance
        active = True
        position = last
    if active:
        outings += 1
    if visits + completed_visits > policy.max_depot_cycles:
        issues.append("depot_cycle_service_limit_exceeded")
    if hypot(*position.to_local_meters(candidate.disposal_depot)) > 1.0:
        issues.append("depot_cycle_final_return_missing")
    minimum = min(minimum, battery)
    max_minutes, max_distance = max(max_minutes, elapsed), max(max_distance, distance)
    return dict(
        issues=list(dict.fromkeys(issues)), max_outing_duration_minutes=max_minutes,
        max_outing_distance_m=max_distance, minimum_battery_percent=minimum,
        remaining_battery_percent=battery, outing_count=outings, depot_visits=visits,
        charging_duration_minutes=service_minutes, duration_minutes=total_minutes,
        total_distance_m=total_distance,
        estimated_completion_minutes=clock_start + total_minutes,
    )


class KPIRouteQualityEvaluator:
    def evaluate(
        self,
        candidate: RouteCandidate,
        machine: MachineProfile,
        policy: KPIPolicyConfig,
        telemetry_history: Optional[List[TelemetryFrame]] = None,
    ) -> KPIEvaluationResponse:
        metrics: Dict[str, MetricEntry] = {}
        failed: List[str] = []
        recs: List[str] = []
        human_review = False
        # These candidate values are forecasts. Telemetry can inform a separate
        # actuals report, but must never relabel planned estimates as measured.
        prov = DataProvenance.ESTIMATED

        if candidate.task_mode in (TaskType.AREA_SWEEP, TaskType.PATROL, TaskType.HYBRID):
            cov = (
                round(min(100.0, (candidate.covered_area_m2 / candidate.target_area_m2) * 100.0), 2)
                if candidate.target_area_m2 > 0
                else 0.0
            )
            cov_ok = cov >= policy.min_coverage_percent
            metrics["coverage_percent"] = MetricEntry(
                actual=cov, target=policy.min_coverage_percent, passed=cov_ok, provenance=DataProvenance.PLANNED, unit="%"
            )
            if not cov_ok:
                failed.append("coverage_below_target")
                recs.append("Increase swath overlap ratio or adjust sweep angle.")

            missed_pct = max(0.0, 100.0 - cov)
            missed_ok = missed_pct <= policy.max_missed_coverage_percent
            metrics["missed_coverage_percent"] = MetricEntry(
                actual=round(missed_pct, 2), target_max=policy.max_missed_coverage_percent,
                passed=missed_ok, provenance=DataProvenance.PLANNED, unit="%"
            )
            if not missed_ok:
                failed.append("missed_coverage_exceeds_limit")

            repeated_pct = (
                min(100.0, candidate.repeated_area_m2 / max(candidate.target_area_m2, 0.01) * 100.0)
            )
            repeated_ok = repeated_pct <= policy.max_repeated_coverage_percent
            metrics["repeated_coverage_percent"] = MetricEntry(
                actual=round(repeated_pct, 2), target_max=policy.max_repeated_coverage_percent,
                passed=repeated_ok, provenance=DataProvenance.PLANNED, unit="%"
            )
            if not repeated_ok:
                failed.append("repeated_coverage_exceeds_limit")

        actual_elapsed = max((frame.mission_elapsed_min for frame in (telemetry_history or [])), default=candidate.mission_elapsed_min)
        cycles = replay_depot_cycles(candidate, machine, policy, actual_elapsed) if candidate.depot_cycles_enabled else None
        duration = cycles["duration_minutes"] if cycles else candidate.estimated_duration_minutes
        dur_ok = (cycles["max_outing_duration_minutes"] if cycles else duration) <= policy.max_estimated_duration_minutes
        metrics["estimated_duration_minutes"] = MetricEntry(
            actual=duration,
            target_max=None if cycles else policy.max_estimated_duration_minutes,
            passed=True if cycles else dur_ok,
            is_constraint=not bool(cycles),
            provenance=prov,
            unit="min",
        )
        if not dur_ok:
            failed.append("duration_exceeds_target_max")
        if cycles:
            failed.extend(issue for issue in cycles["issues"] if issue not in failed)
            for key, unit in (("max_outing_duration_minutes", "min"), ("max_outing_distance_m", "m"),
                              ("minimum_battery_percent", "%"), ("outing_count", "outings"),
                              ("depot_visits", "visits"), ("charging_duration_minutes", "min"),
                              ("estimated_completion_minutes", "min")):
                limit = policy.max_estimated_duration_minutes if key == "max_outing_duration_minutes" else policy.max_travel_distance_m if key == "max_outing_distance_m" else None
                reserve = policy.min_energy_reserve_percent if key == "minimum_battery_percent" else None
                metrics[key] = MetricEntry(actual=round(cycles[key], 3), unit=unit,
                    target_max=limit, target_min=reserve,
                    is_constraint=limit is not None or reserve is not None,
                    passed=(limit is None or cycles[key] <= limit) and (reserve is None or cycles[key] + 1e-6 >= reserve),
                    provenance=prov)

        window_misses = [
            item for item in candidate.skipped_tasks
            if "task_time_window_infeasible" in item.get("reasons", [])
        ]
        window_ok = len(window_misses) == 0
        metrics["time_window_violations"] = MetricEntry(
            actual=float(len(window_misses)), target_max=0.0, passed=window_ok,
            provenance=DataProvenance.PLANNED, unit="tasks",
        )
        if not window_ok:
            failed.append("task_time_window_infeasible")

        geometry_ok = not candidate.route_safety_issues and not candidate.unreachable_sections
        metrics["route_geometry_issues"] = MetricEntry(
            actual=float(len(candidate.route_safety_issues) + len(candidate.unreachable_sections)),
            target_max=0.0, passed=geometry_ok, provenance=DataProvenance.PLANNED, unit="sections",
        )
        if not geometry_ok:
            failed.append("route_geometry_unresolved")

        frames = telemetry_history or []
        telemetry_provenance = (
            DataProvenance.MEASURED_TELEMETRY
            if any(frame.provenance == DataProvenance.MEASURED_TELEMETRY for frame in frames)
            else DataProvenance.SIMULATED if frames else prov
        )
        max_deviation = max((frame.route_deviation_m for frame in frames), default=0.0)
        obstacle_count = sum(frame.obstacle_blocked for frame in frames)
        deviation_ok = max_deviation <= policy.max_route_deviation_m
        metrics["max_route_deviation_m"] = MetricEntry(
            actual=round(max_deviation, 2), target_max=policy.max_route_deviation_m,
            passed=deviation_ok, provenance=telemetry_provenance, unit="m",
        )
        obstacle_ok = obstacle_count == 0
        metrics["obstacle_events"] = MetricEntry(
            actual=float(obstacle_count), target_max=0.0, passed=obstacle_ok,
            provenance=telemetry_provenance, unit="events",
        )
        if not deviation_ok:
            failed.append("route_deviation_exceeds_limit")
        if not obstacle_ok:
            failed.append("obstacle_event_requires_replan")

        remaining = cycles["remaining_battery_percent"] if cycles else candidate.estimated_remaining_battery_percent
        batt_ok = (cycles["minimum_battery_percent"] if cycles else remaining) + 1e-6 >= policy.min_energy_reserve_percent
        metrics["remaining_energy_reserve_percent"] = MetricEntry(
            actual=remaining,
            target_min=policy.min_energy_reserve_percent,
            passed=batt_ok,
            provenance=prov,
            unit="%",
        )
        if not batt_ok and "energy_reserve_below_minimum" not in failed:
            failed.append("energy_reserve_below_minimum")

        distance_ok = (cycles["max_outing_distance_m"] if cycles else candidate.total_distance_m) <= policy.max_travel_distance_m
        metrics["total_distance_m"] = MetricEntry(
            actual=cycles["total_distance_m"] if cycles else candidate.total_distance_m,
            target_max=None if cycles else policy.max_travel_distance_m,
            passed=True if cycles else distance_ok, is_constraint=not bool(cycles),
            provenance=DataProvenance.ESTIMATED, unit="m"
        )
        if not distance_ok:
            failed.append("travel_distance_exceeds_limit")

        payload_ok = candidate.payload_utilization_percent <= policy.max_payload_utilization_percent
        metrics["payload_utilization_percent"] = MetricEntry(
            actual=candidate.payload_utilization_percent, target_max=policy.max_payload_utilization_percent,
            passed=payload_ok, provenance=DataProvenance.ESTIMATED, unit="%"
        )
        if not payload_ok:
            failed.append("payload_utilization_exceeds_limit")

        collection_tasks = [
            t for t in candidate.tasks
            if t.task_type in (TaskType.RUBBISH_COLLECTION, TaskType.OVERSIZED_RUBBISH)
        ]
        task_ids = {t.task_id for t in collection_tasks}
        task_ids.update(item.get("task_id", "") for item in candidate.skipped_tasks)
        task_ids.discard("")
        assigned_ids = {entry.task_id for entry in candidate.task_schedule}
        assigned_ids.update(task.task_id for task in collection_tasks if task.completed)
        assigned_count = len(task_ids.intersection(assigned_ids))
        total_collection_tasks = len(task_ids)
        assignment_percent = (
            100.0 if total_collection_tasks == 0
            else 100.0 * assigned_count / total_collection_tasks
        )
        collection_ok = assignment_percent >= policy.required_collection_completion_rate
        assignment_metric = MetricEntry(
            actual=assignment_percent, target=policy.required_collection_completion_rate,
            passed=collection_ok, provenance=DataProvenance.PLANNED, unit="%",
        )
        metrics["planned_collection_assignment_percent"] = assignment_metric
        # Keep the original metric key for API clients that predate the clearer name.
        metrics["collection_completion_percent"] = assignment_metric.model_copy(deep=True)
        completed_count = sum(task.task_id in task_ids and task.completed for task in collection_tasks)
        simulated_completion = (
            100.0 if total_collection_tasks == 0
            else 100.0 * completed_count / total_collection_tasks
        )
        metrics["simulated_collection_completion_percent"] = MetricEntry(
            actual=simulated_completion,
            target=policy.required_collection_completion_rate,
            passed=simulated_completion >= policy.required_collection_completion_rate,
            is_constraint=False,
            provenance=(
                DataProvenance.SIMULATED if any(frame.provenance == DataProvenance.SIMULATED for frame in frames)
                else DataProvenance.MEASURED_TELEMETRY if frames
                else DataProvenance.PLANNED
            ),
            unit="%",
        )
        if not collection_ok:
            failed.append("collection_completion_below_target")

        cov_score = metrics.get("coverage_percent", MetricEntry(actual=100.0, passed=True)).actual
        score = round(0.6 * cov_score + 0.4 * remaining, 2)

        return KPIEvaluationResponse(
            route_version=candidate.route_version,
            policy_version=policy.policy_version,
            passed=(len(failed) == 0),
            score=score,
            metrics=metrics,
            failed_constraints=failed,
            recommended_adjustments=recs,
            requires_human_review=human_review,
        )
