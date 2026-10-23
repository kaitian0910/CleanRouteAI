"""Explain fresh repeat planning without confusing new IDs with new geometry."""
from .models import RepeatPlanComparison, TaskType


def _point(point):
    return (round(point.lat, 7), round(point.lon, 7))


def _path(route, sweep_only=False):
    return [(segment.segment_type, tuple(_point(point) for point in segment.waypoints))
            for segment in route.segments
            if not sweep_only or segment.segment_type == "SWEEP_SWATH"]


def _collection_order(route):
    # Match recurring work by location, not the fresh IDs assigned on repeat.
    bins = {task.task_id: _point(task.location) for task in route.tasks
            if task.task_type == TaskType.RUBBISH_COLLECTION and task.waste_observation is None
            and not (task.rubbish_spec and task.rubbish_spec.is_oversized)}
    return [bins[entry.task_id] for entry in route.task_schedule if entry.task_id in bins]


def compare_repeat_plan(source, repeated, previous_elapsed=None):
    sweep_changed = _path(source, True) != _path(repeated, True)
    order_changed = _collection_order(source) != _collection_order(repeated)
    travel_changed = _path(source) != _path(repeated)
    checked = len(repeated.rl_feedback)
    if sweep_changed or order_changed:
        summary = "A fresh route was selected after checking the current workload, battery and access limits."
    elif travel_changed:
        summary = "The cleaning pattern was retained; travel or depot movements changed for this run."
    else:
        summary = "The same path was selected again from the options checked. An unchanged path can still be the best feasible option checked."
    if repeated.history_hint_applied:
        summary += " Previous run feedback suggested the first sweep setting to check; fresh options were independently evaluated with current resources."
    summary += " Past recorded simulation time and the new estimate are shown separately; they do not prove a real-world time saving."
    previous_estimate = source.estimated_duration_minutes + source.mission_elapsed_min
    return RepeatPlanComparison(
        previous_route_id=source.route_id, candidates_checked=checked,
        feasible_candidates=sum(record.feasible for record in repeated.rl_feedback),
        previous_estimated_completion_minutes=previous_estimate,
        previous_actual_completion_minutes=previous_elapsed,
        new_estimated_completion_minutes=repeated.estimated_completion_minutes,
        estimated_time_change_minutes=round(repeated.estimated_completion_minutes - previous_estimate, 3),
        sweep_pattern_changed=sweep_changed, collection_order_changed=order_changed,
        travel_path_changed=travel_changed, history_applied=repeated.history_hint_applied,
        summary=summary,
    )
