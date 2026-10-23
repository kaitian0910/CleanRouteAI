import type { RouteCandidate } from "./types/api";

export function formatMinutes(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "N/A" : `${value.toFixed(1)} min`;
}

export function newTaskWindow(elapsedMinutes: number, windowMinutes = 90) {
  const start = Number.isFinite(elapsedMinutes) ? Math.max(0, elapsedMinutes) : 0;
  return { time_window_start_min: start, time_window_end_min: start + windowMinutes };
}

function geometryBlockMessage(issues: string[]): string {
  const details = [...new Set(issues.filter(issue => issue.trim()))];
  const reasons = details.join(" ");
  const messages = ["The planner could not produce a safe route connection."];
  if (/water_body|water body|mapped water|impassable water/i.test(reasons)) {
    messages.push("A route segment or its required clearance reaches mapped water.");
  }
  if (/leaves operating boundary|outside.*boundary|leaves operator-declared traversable footprint/i.test(reasons)) {
    messages.push("A route segment or its required clearance extends outside the selected operating area.");
  }
  if (/unreachable|disconnected|no permitted.*path|no validated path/i.test(reasons)) {
    messages.push("Some points have no safe connecting path.");
  }
  messages.push("The route remains blocked until a safe path is available.");
  if (details.length) {
    messages.push(`Details: ${details.slice(0, 3).map(issue => issue.length > 240 ? `${issue.slice(0, 237)}...` : issue).join("; ")}.`);
    if (details.length > 3) messages.push(`${details.length - 3} more route safety ${details.length === 4 ? "issue" : "issues"}.`);
  }
  return messages.join(" ");
}

export function planningBlockMessage(route: RouteCandidate): string {
  const evaluation = route.kpi_evaluation;
  if (!evaluation) return "The route has not been evaluated.";
  const messages: string[] = [];
  const failed = evaluation.failed_constraints;
  const duration = route.depot_cycles_enabled
    ? evaluation.metrics.max_outing_duration_minutes || evaluation.metrics.estimated_duration_minutes
    : evaluation.metrics.estimated_duration_minutes;
  const battery = route.depot_cycles_enabled
    ? evaluation.metrics.minimum_battery_percent || evaluation.metrics.remaining_energy_reserve_percent
    : evaluation.metrics.remaining_energy_reserve_percent;
  if (failed.includes("duration_exceeds_target_max") && duration) {
    messages.push(`${route.depot_cycles_enabled ? "Longest outing duration" : "Estimated duration"} ${duration.actual.toFixed(1)} min exceeds the ${duration.target_max ?? route.kpi_policy_snapshot?.max_estimated_duration_minutes ?? 90} min limit.`);
  }
  if (failed.includes("energy_reserve_below_minimum") && battery) {
    messages.push(`${route.depot_cycles_enabled ? "Lowest estimated outing battery" : "Estimated battery at finish"} is ${battery.actual.toFixed(1)}%; the required reserve is ${battery.target_min ?? route.kpi_policy_snapshot?.min_energy_reserve_percent ?? 20}%.`);
  }
  if (messages.length) messages.push(route.depot_cycles_enabled
    ? "Depot returns are enabled, but at least one outing cannot safely fit its battery, duration or travel limit. Check depot access and the failed outing; a safe return path must fit before work can resume."
    : "Enable return to depot, recharge & resume to split the job into safe outings, or select a smaller area for this mission. Adding an obstacle recalculates the path, but does not increase battery capacity or the time limit.");
  const missedTaskIds = new Set((route.skipped_tasks ?? []).filter(item => item.reasons?.some(reason => reason.includes("time_window"))).map(item => item.task_id));
  for (const entry of route.task_schedule ?? []) {
    if (entry.service_end_min > entry.window_end_min + 0.02 || /MISSED|INFEASIBLE|LATE/.test(entry.status)) missedTaskIds.add(entry.task_id);
  }
  const isDeadlineFailure = (key: string) => key === "time_window_violation" || key === "task_time_window_infeasible" || key.startsWith("depot_cycle_task_window_missed");
  if (failed.some(isDeadlineFailure) || missedTaskIds.size) {
    messages.push("Collection deadlines could not be met; the route remains blocked until the schedule is feasible.");
    for (const taskId of [...missedTaskIds].slice(0, 4)) {
      const task = route.tasks?.find(item => item.task_id === taskId);
      const entry = route.task_schedule?.find(item => item.task_id === taskId);
      const taskName = task?.waste_observation?.waste_class ? `${taskId} (${task.waste_observation.waste_class})` : taskId;
      const start = task?.time_window_start_min ?? entry?.window_start_min;
      const end = task?.time_window_end_min ?? entry?.window_end_min;
      messages.push(`${taskName}: ${entry ? `arrival ${formatMinutes(entry.arrival_min)}, service finish ${formatMinutes(entry.service_end_min)}` : "no feasible service slot"}; required window ${formatMinutes(start)} to ${formatMinutes(end)} since job start${task ? `, including ${task.service_duration_sec.toFixed(0)} s service` : ""}.`);
    }
    if (missedTaskIds.size > 4) messages.push(`${missedTaskIds.size - 4} more missed collection deadlines.`);
    messages.push("Review each task's deadline and service time. Depot recharges count toward elapsed job time; the planner must collect deadline-sensitive bins before long sweeping work.");
  }
  if (failed.includes("collection_completion_below_target")) {
    const collectionTasks = (route.tasks ?? []).filter(task => ["RUBBISH_COLLECTION", "OVERSIZED_RUBBISH"].includes(task.task_type));
    const assigned = new Set((route.task_schedule ?? []).filter(entry => !missedTaskIds.has(entry.task_id)).map(entry => entry.task_id));
    const count = collectionTasks.filter(task => task.completed || assigned.has(task.task_id)).length;
    messages.push(collectionTasks.length ? `${count}/${collectionTasks.length} collection tasks are completed or assigned a feasible service slot; the required collection target is not met.` : "The required collection completion target is not met.");
  }
  if (failed.includes("route_geometry_unresolved")) {
    messages.push(geometryBlockMessage(route.route_safety_issues ?? []));
  }
  const remaining = failed.filter(key => !["duration_exceeds_target_max", "energy_reserve_below_minimum", "collection_completion_below_target", "route_geometry_unresolved"].includes(key) && !isDeadlineFailure(key));
  if (remaining.length) messages.push(`Other failed checks: ${remaining.join(", ")}.`);
  return messages.join(" ") || route.route_safety_issues.join("; ") || "The route failed a required planning check.";
}
