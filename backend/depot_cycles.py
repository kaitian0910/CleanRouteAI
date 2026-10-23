"""Turn a geometric work route into bounded, depot-serviced battery outings.

Connectors are supplied by the existing traversability graph. No straight-line
fallback is permitted: a failed return path keeps the candidate unapprovable.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from .config import KPIPolicyConfig
from .models import Coordinate, MachineProfile, RouteCandidate, RouteSegment, TaskScheduleEntry


def _distance(points: List[Coordinate]) -> float:
    return sum((sum(v * v for v in b.to_local_meters(a))) ** 0.5
               for a, b in zip(points, points[1:]))


def _near(a: Coordinate, b: Coordinate) -> bool:
    return _distance([a, b]) <= 0.5


def _pieces(segment: RouteSegment, limit: float = 200.0) -> List[RouteSegment]:
    """Partition a polyline without changing its swept centreline or width."""
    if segment.completed or segment.segment_type == "RETURN_TO_BASE" or segment.distance_m <= limit or len(segment.waypoints) < 2:
        return [segment.model_copy(deep=True)]
    lengths = [_distance([a, b]) for a, b in zip(segment.waypoints, segment.waypoints[1:])]
    geometric_length = sum(lengths)
    if geometric_length < 1e-6:
        return [segment.model_copy(deep=True)]
    # Partition in geometric units, preserving the original distance/time totals.
    geometric_limit = limit * geometric_length / segment.distance_m
    groups: List[List[Coordinate]] = []
    group = [segment.waypoints[0]]
    used = 0.0
    for a, b, length in zip(segment.waypoints, segment.waypoints[1:], lengths):
        consumed = 0.0
        while length - consumed > 1e-8:
            take = min(length - consumed, geometric_limit - used)
            consumed += take
            used += take
            fraction = consumed / length
            point = b if fraction >= 1 - 1e-10 else Coordinate(
                lat=a.lat + (b.lat - a.lat) * fraction,
                lon=a.lon + (b.lon - a.lon) * fraction,
            )
            group.append(point)
            if used >= geometric_limit - 1e-8:
                groups.append(group)
                group = [point]
                used = 0.0
    if len(group) > 1:
        groups.append(group)
    result = []
    for index, points in enumerate(groups):
        ratio = _distance(points) / geometric_length
        result.append(segment.model_copy(update={
            "segment_id": f"{segment.segment_id}-part-{index + 1}",
            "waypoints": points, "distance_m": segment.distance_m * ratio,
            "estimated_duration_sec": segment.estimated_duration_sec * ratio,
        }, deep=True))
    return result


def plan_depot_cycles(
    candidate: RouteCandidate, machine: MachineProfile, policy: KPIPolicyConfig,
    connector: Callable[[Coordinate, Coordinate], Optional[List[Coordinate]]],
) -> RouteCandidate:
    """Keep work order/coverage, inserting safe return, service and exact resume.

    Limits apply independently to each outing; total completion time includes
    every detour and depot service. Unsafe/unreachable work is retained as a
    blocked draft, with an explicit safety issue rather than silently omitted.
    """
    depot = candidate.disposal_depot
    candidate.initial_battery_percent = machine.current_battery_percent
    original = candidate.segments
    completed = [s for s in original if s.completed]
    completed_visits = sum(s.segment_type == "DEPOT_RECHARGE" for s in completed)
    pending = [p for s in original if not s.completed and s.segment_type != "WAIT_FOR_WINDOW"
               for p in _pieces(s)]
    for segment in pending:
        segment.distance_m = max(segment.distance_m, _distance(segment.waypoints))
        speed = machine.sweep_speed_mps if segment.segment_type == "SWEEP_SWATH" else machine.cruise_speed_mps
        segment.estimated_duration_sec = max(segment.estimated_duration_sec, segment.distance_m / speed)
    result = list(completed)
    tasks = {t.task_id: t for t in candidate.tasks}
    completed_ids = {s.target_task_id for s in completed if s.segment_type == "COLLECTION_STOP"}
    schedules = [s for s in candidate.task_schedule if s.task_id in completed_ids]
    current = candidate.origin
    battery = machine.current_battery_percent
    reserve = policy.min_energy_reserve_percent
    outing = completed[-1].outing_index if completed else 1
    outing_distance = outing_seconds = 0.0
    # Replanning starts from measured battery/location, but the current outing
    # already spent time and distance before that measurement. Preserve those
    # budgets without adding historical time to the future completion estimate.
    for historical in completed:
        if historical.segment_type == "DEPOT_RECHARGE":
            outing_distance = outing_seconds = 0.0
        else:
            outing_distance += max(historical.distance_m, _distance(historical.waypoints))
            outing_seconds += historical.estimated_duration_sec
    if completed and completed[-1].segment_type == "DEPOT_RECHARGE":
        outing += 1
    maximum_distance = maximum_seconds = 0.0
    minimum_battery = battery
    elapsed = candidate.mission_elapsed_min * 60.0
    charge_minutes = 0.0
    visits = 0
    paths = {}

    def issue(reason: str) -> None:
        text = f"depot_cycle_{reason}"
        if text not in candidate.route_safety_issues:
            candidate.route_safety_issues.append(text)

    def travel(a: Coordinate, b: Coordinate, kind: str = "TRANSIT") -> Optional[RouteSegment]:
        if _near(a, b):
            points = [a, b]
        else:
            key = (a.lat, a.lon, b.lat, b.lon)
            if key not in paths:
                paths[key] = connector(a, b)
            points = paths[key]
            if not points or not _near(points[0], a) or not _near(points[-1], b):
                return None
        distance = _distance(points)
        return RouteSegment(segment_type=kind, waypoints=points,
                            distance_m=distance,
                            estimated_duration_sec=distance / machine.cruise_speed_mps)

    def energy(segment: RouteSegment) -> float:
        rate = (machine.energy_consumption_kwh_per_km_sweep
                if segment.segment_type == "SWEEP_SWATH"
                else machine.energy_consumption_kwh_per_km_travel)
        return segment.distance_m / 1000.0 * rate / machine.battery_capacity_kwh * 100.0

    def fits(segments: List[RouteSegment]) -> bool:
        return (battery - sum(energy(s) for s in segments) >= reserve - 1e-7
                and outing_distance + sum(s.distance_m for s in segments) <= policy.max_travel_distance_m + 1e-7
                and outing_seconds + sum(s.estimated_duration_sec for s in segments)
                <= policy.max_estimated_duration_minutes * 60.0 + 1e-7)

    def append(segment: RouteSegment) -> None:
        nonlocal current, battery, minimum_battery, elapsed, outing_distance, outing_seconds
        segment.outing_index = outing
        result.append(segment)
        battery -= energy(segment)
        minimum_battery = min(minimum_battery, battery)
        elapsed += segment.estimated_duration_sec
        outing_distance += segment.distance_m
        outing_seconds += segment.estimated_duration_sec
        if segment.waypoints:
            current = segment.waypoints[-1]

    def recharge() -> bool:
        nonlocal battery, outing, outing_distance, outing_seconds, maximum_distance
        nonlocal maximum_seconds, elapsed, visits, charge_minutes, minimum_battery
        if completed_visits + visits >= policy.max_depot_cycles:
            issue("limit_exceeded")
            return False
        maximum_distance = max(maximum_distance, outing_distance)
        maximum_seconds = max(maximum_seconds, outing_seconds)
        service = RouteSegment(segment_type="DEPOT_RECHARGE", waypoints=[depot],
                               distance_m=0.0, estimated_duration_sec=machine.depot_service_minutes * 60.0,
                               outing_index=outing)
        result.append(service)
        elapsed += service.estimated_duration_sec
        charge_minutes += machine.depot_service_minutes
        visits += 1
        battery = machine.recharged_battery_percent
        # An already depleted machine may be serviced while stationary at its depot.
        if len(result) == len(completed) + 1:
            minimum_battery = battery
        outing += 1
        outing_distance = outing_seconds = 0.0
        return True

    if pending and _near(current, depot) and battery < reserve:
        recharge()
    for index, segment in enumerate(pending):
        if not segment.waypoints:
            issue("missing_waypoints")
            result.extend(pending[index:])
            break
        task = tasks.get(segment.target_task_id or "") if segment.segment_type == "COLLECTION_STOP" else None

        def block() -> List[RouteSegment]:
            wait = max(0.0, task.time_window_start_min * 60.0 - elapsed) if task else 0.0
            if wait <= 1e-7:
                return [segment]
            return [RouteSegment(segment_type="WAIT_FOR_WINDOW", waypoints=[current],
                                 distance_m=0.0, estimated_duration_sec=wait,
                                 target_task_id=task.task_id), segment]

        end = segment.waypoints[-1]
        home = travel(end, depot, "RETURN_TO_BASE")
        if home is None:
            issue("return_unreachable")
            result.extend(pending[index:])
            break
        work = block()
        if not fits(work + [home]):
            return_now = travel(current, depot, "RETURN_TO_BASE")
            if return_now is None or not fits([return_now]):
                issue("cannot_return_with_reserve")
                result.extend(pending[index:])
                break
            resume = current
            if not _near(current, depot):
                append(return_now)
            if not recharge():
                result.extend(pending[index:])
                break
            restart = travel(depot, resume)
            work = block()
            # Evaluate waiting after outbound travel rather than at the depot.
            if task and restart is not None:
                wait = max(0.0, task.time_window_start_min * 60.0 - elapsed - restart.estimated_duration_sec)
                work = ([RouteSegment(segment_type="WAIT_FOR_WINDOW", waypoints=[resume], distance_m=0.0,
                                      estimated_duration_sec=wait, target_task_id=task.task_id)] if wait > 1e-7 else []) + [segment]
            if restart is None or not fits([restart] + work + [home]):
                issue("work_unreachable_within_outing_limits")
                result.extend(pending[index:])
                break
            if restart.distance_m > 1e-7:
                append(restart)
        arrival = elapsed
        for part in work:
            append(part)
        if task:
            service_start = elapsed / 60.0 - segment.estimated_duration_sec / 60.0
            late = elapsed / 60.0 > task.time_window_end_min + 1e-7
            schedules.append(TaskScheduleEntry(
                task_id=task.task_id, arrival_min=arrival / 60.0,
                service_start_min=service_start, service_end_min=elapsed / 60.0,
                waiting_min=service_start - arrival / 60.0,
                window_start_min=task.time_window_start_min, window_end_min=task.time_window_end_min,
                priority=task.priority, status="INFEASIBLE" if late else "SCHEDULED",
                reason="depot_service_delays_task_window" if late else None,
            ))
            if late:
                issue(f"task_window_missed:{task.task_id}")
        if segment.segment_type == "RETURN_TO_BASE" and _near(current, depot) and index + 1 < len(pending):
            if not recharge():
                result.extend(pending[index + 1:])
                break
    else:
        if pending and not _near(current, depot):
            final = travel(current, depot, "RETURN_TO_BASE")
            if final is None or not fits([final]):
                issue("final_return_unreachable")
            else:
                append(final)

    remaining = [s for s in result if not s.completed]
    sweep = sum(s.distance_m for s in remaining if s.segment_type == "SWEEP_SWATH")
    transit = sum(s.distance_m for s in remaining if s.segment_type != "SWEEP_SWATH")
    duration = sum(s.estimated_duration_sec for s in remaining) / 60.0
    candidate.segments = result
    candidate.task_schedule = schedules
    candidate.depot_cycles_enabled = True
    # Public whole-job totals include retained history as well as future work.
    # The local visits/charge_minutes values above stay limited to remaining
    # service, and the independent evaluator replays remaining resources.
    candidate.depot_visits = sum(s.segment_type == "DEPOT_RECHARGE" for s in result)
    candidate.charging_duration_minutes = sum(
        s.estimated_duration_sec / 60.0 for s in result if s.segment_type == "DEPOT_RECHARGE"
    )
    candidate.max_outing_distance_m = max(maximum_distance, outing_distance)
    candidate.max_outing_duration_minutes = max(maximum_seconds, outing_seconds) / 60.0
    candidate.minimum_battery_percent = max(0.0, minimum_battery)
    candidate.estimated_remaining_battery_percent = max(0.0, battery)
    candidate.total_distance_m = sweep + transit
    candidate.sweep_distance_m = sweep
    candidate.transit_distance_m = transit
    candidate.estimated_energy_kwh = sum(energy(s) * machine.battery_capacity_kwh / 100.0 for s in remaining)
    candidate.estimated_duration_minutes = duration
    candidate.estimated_completion_minutes = candidate.mission_elapsed_min + duration
    candidate.outing_count = len({s.outing_index for s in result
        if s.segment_type != "DEPOT_RECHARGE"
        and (s.distance_m > 1e-7 or s.estimated_duration_sec > 1e-7)})
    candidate.disposal_trips = sum(s.segment_type == "RETURN_TO_BASE" for s in remaining)
    candidate.candidate_rationale.append(
        f"Remaining depot schedule: {visits} service stops; remaining completion {duration:.1f} minutes "
        f"including {charge_minutes:.1f} minutes at the depot. Each outing keeps a safe return reserve."
    )
    return candidate
