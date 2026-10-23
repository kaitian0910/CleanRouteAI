import type { MetricEntry, RouteCandidate, TaskItem, TaskScheduleEntry, TelemetryFrame } from "./types/api";
import "./PlanChecks.css";

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const displayNumber = (value: unknown, digits = 1) => finite(value)
  ? value.toLocaleString("en", { maximumFractionDigits: digits }) : "Not available";
const firstNumber = (...values: unknown[]): number | undefined => values.find(finite) as number | undefined;

function Meter({ value, limit, label, reverse = false }: { value: unknown; limit: unknown; label: string; reverse?: boolean }) {
  if (!finite(value) || !finite(limit) || limit <= 0) return null;
  const max = reverse ? Math.max(100, value, limit) : Math.max(value, limit);
  const width = Math.max(0, Math.min(100, value / max * 100));
  const marker = Math.max(0, Math.min(100, limit / max * 100));
  const passed = reverse ? value + 0.000001 >= limit : value <= limit;
  return <div className={`plan-check-meter ${passed ? "is-good" : "is-bad"}`} role="meter" aria-label={label}
    aria-valuemin={0} aria-valuemax={max} aria-valuenow={Math.max(0, value)}>
    <span style={{ width: `${width}%` }} />
    <i style={{ left: `${marker}%` }} title={`Configured limit: ${displayNumber(limit)}`} />
  </div>;
}

function CheckRow({ label, metric, metricName }: { label: string; metric?: MetricEntry; metricName: string }) {
  const available = metric && finite(metric.actual) && typeof metric.passed === "boolean";
  const threshold = metric ? firstNumber(metric.target_min, metric.target_max, metric.target) : undefined;
  const constraint = metric?.target_min != null ? "minimum" : metric?.target_max != null ? "maximum" : "target";
  return <li>
    <div><strong>{label}</strong><small>{available ? `${displayNumber(metric.actual)} ${metric.unit || ""}${finite(threshold) ? ` · ${constraint} ${displayNumber(threshold)} ${metric.unit || ""}` : ""}` : "No evaluation recorded"}</small></div>
    <span className={`plan-check-status ${available ? metric.passed ? "is-good" : "is-bad" : ""}`}>{available ? metric.passed ? "Passed" : "Needs attention" : "Not checked"}</span>
    {available && <details><summary>Check source</summary><code>{metricName}</code><span> · {typeof metric.provenance === "string" ? metric.provenance.toLowerCase().replace(/_/g, " ") : "source not recorded"}</span></details>}
  </li>;
}

export interface PlanChecksProps {
  route: RouteCandidate | null;
  telemetry: TelemetryFrame | null;
  onOpenRoute?: () => void;
}

export function PlanChecks({ route, telemetry, onOpenRoute }: PlanChecksProps) {
  if (!route) return <section className="plan-checks"><h2>Plan checks</h2><p className="plan-check-note">Choose an area and create a plan to see its battery, time and access checks.</p></section>;
  const evaluation = route.kpi_evaluation && route.kpi_evaluation.route_version === route.route_version ? route.kpi_evaluation : null;
  const metrics = evaluation?.metrics ?? {};
  const policy = route.kpi_policy_snapshot ?? {};
  const cycles = Boolean(route.depot_cycles_enabled);
  const reserve = firstNumber(policy.min_energy_reserve_percent, metrics.minimum_battery_percent?.target_min, metrics.remaining_energy_reserve_percent?.target_min);
  const battery = cycles ? firstNumber(route.minimum_battery_percent, metrics.minimum_battery_percent?.actual)
    : firstNumber(route.estimated_remaining_battery_percent, metrics.remaining_energy_reserve_percent?.actual);
  const durationLimit = firstNumber(policy.max_estimated_duration_minutes, metrics.max_outing_duration_minutes?.target_max, metrics.estimated_duration_minutes?.target_max);
  const duration = cycles ? firstNumber(route.max_outing_duration_minutes, metrics.max_outing_duration_minutes?.actual)
    : firstNumber(route.estimated_duration_minutes, metrics.estimated_duration_minutes?.actual);
  const payload = firstNumber(metrics.payload_utilization_percent?.actual, route.payload_utilization_percent);
  const payloadLimit = firstNumber(policy.max_payload_utilization_percent, metrics.payload_utilization_percent?.target_max);
  const current = telemetry?.route_id === route.route_id && telemetry.machine_id === route.machine_id ? telemetry : null;
  const measured = current?.provenance === "MEASURED_TELEMETRY";
  return <section className="plan-checks">
    <div className="plan-check-heading"><div><h2>Can this plan be completed?</h2><p className="plan-check-note">Planned checks use the configured machine and site data. Execution is currently simulated.</p></div>
      <span className={`plan-check-status ${evaluation ? evaluation.passed ? "is-good" : "is-bad" : ""}`}>{evaluation ? evaluation.passed ? "Planned checks passed" : "Plan needs attention" : "Not evaluated yet"}</span>
    </div>
    <div className="plan-check-grid">
      <article className="plan-check-card"><h3>{cycles ? "Lowest planned battery" : "Battery at finish (estimate)"}</h3>
        <div className="plan-check-value">{displayNumber(battery)}{finite(battery) ? "%" : ""}<small>Required reserve: {displayNumber(reserve)}{finite(reserve) ? "%" : ""}</small></div>
        <Meter value={battery} limit={reserve} reverse label="Planned battery and required reserve" />
        <p>{finite(current?.battery_percent) ? `${measured ? "Latest reported" : "Latest simulated"} battery: ${displayNumber(current.battery_percent)}%.` : "No current battery reading for this run."} The marker shows the configured reserve.</p>
      </article>
      <article className="plan-check-card"><h3>{cycles ? "Longest planned outing" : "Planned trip time"}</h3>
        <div className="plan-check-value">{displayNumber(duration)}{finite(duration) ? " min" : ""}<small>Limit: {displayNumber(durationLimit)}{finite(durationLimit) ? " min per outing" : ""}</small></div>
        <Meter value={duration} limit={durationLimit} label="Planned outing time and configured limit" />
        <p>{cycles ? "The robot can return for service and continue. The whole job may take longer than one outing." : "This plan does not split work into recharge outings."}</p>
      </article>
      <article className="plan-check-card"><h3>Waste load</h3>
        <div className="plan-check-value">{displayNumber(payload)}{finite(payload) ? "%" : ""}<small>Planned capacity use{finite(payloadLimit) ? ` · limit ${displayNumber(payloadLimit)}%` : " · limit not recorded"}</small></div>
        <Meter value={payload} limit={payloadLimit} label="Estimated payload capacity use" />
        <p>{finite(current?.payload_kg) ? `${measured ? "Latest reported" : "Latest simulated"} load: ${displayNumber(current.payload_kg)} kg${finite(current.payload_volume_m3) ? ` / ${displayNumber(current.payload_volume_m3, 3)} m³` : ""}.` : "No current load reading for this run."} Photo classifications do not measure weight or hopper capacity.</p>
      </article>
    </div>
    <article className="plan-check-card plan-check-list-card"><h3>Access, coverage and collections</h3><ul className="plan-check-list">
      <CheckRow label="Access and obstacle clearance" metric={metrics.route_geometry_issues} metricName="route_geometry_issues" />
      <CheckRow label="Area coverage" metric={metrics.coverage_percent} metricName="coverage_percent" />
      <CheckRow label="Collection tasks included in the plan" metric={metrics.planned_collection_assignment_percent} metricName="planned_collection_assignment_percent" />
      <CheckRow label="Collection deadlines" metric={metrics.time_window_violations} metricName="time_window_violations" />
    </ul><p className="plan-check-note">A task included in a plan is not yet a completed pickup. These checks do not verify physical site conditions.</p></article>
    {Boolean(evaluation?.failed_constraints?.length) && <details className="plan-check-issues"><summary>Recorded checks needing attention ({evaluation!.failed_constraints.length})</summary><ul>{evaluation!.failed_constraints.map((issue, index) => <li key={`${issue}-${index}`}><code>{issue}</code></li>)}</ul></details>}
    <div className="plan-check-footer"><p className="plan-check-note">Limits shown here come from this plan's saved settings.</p>{onOpenRoute && <button type="button" onClick={onOpenRoute}>Edit limits on Route map</button>}</div>
  </section>;
}

const taskTypeName = (task?: TaskItem) => task?.task_type === "OVERSIZED_RUBBISH" ? "Bulky pickup"
  : task?.task_type === "AREA_SWEEP" ? "Area cleaning" : task?.task_type === "PATROL" ? "Patrol"
    : task?.waste_observation ? "Litter pickup" : "Bin pickup";
const time = (minutes: unknown) => finite(minutes) ? `${displayNumber(minutes)} min` : "Not recorded";
const seconds = (value: unknown) => finite(value) ? value < 60 ? `${displayNumber(value)} sec` : `${displayNumber(value / 60)} min` : "Not recorded";
const reasonMessage = (reasons: string[]) => reasons.some(reason => /time_window|deadline/.test(reason)) ? "The task's time window cannot be met by this plan."
  : reasons.some(reason => /payload|capacity|heavy_lift|oversized|weight|dimension/.test(reason)) ? "This task needs a machine or capacity check."
    : reasons.some(reason => /access|unreachable|obstacle|terrain|geometry/.test(reason)) ? "A safe access path could not be confirmed."
      : "This task was not included. Review the recorded reason before starting.";

export function TaskReview({ route }: { route: RouteCandidate | null }) {
  if (!route) return <section className="plan-checks"><h2>Task review</h2><p className="plan-check-note">Create a cleaning plan to review its collection tasks.</p></section>;
  const tasks = route.tasks ?? [];
  const schedules = new Map<string, TaskScheduleEntry>((route.task_schedule ?? []).map(entry => [entry.task_id, entry]));
  const skipped = new Map((route.skipped_tasks ?? []).map(entry => [entry.task_id, entry]));
  const taskIds = [...new Set([...tasks.map(task => task.task_id), ...skipped.keys()])];
  const rows = taskIds.map((id, index) => {
    const task = tasks.find(item => item.task_id === id);
    const schedule = schedules.get(id);
    const skip = skipped.get(id);
    const done = Boolean(task?.completed);
    const blocked = !done && Boolean(skip);
    const reasons = skip?.reasons ?? [];
    return { id, task, schedule, skip, done, blocked, reasons, label: task?.rubbish_spec?.description?.trim() || `${taskTypeName(task)} ${index + 1}` };
  });
  const finished = rows.filter(row => row.done).length;
  const blocked = rows.filter(row => row.blocked).length;
  return <section className="plan-checks"><div className="plan-check-heading"><div><h2>Task review</h2><p className="plan-check-note">Collection status and the priority recorded for each task. Times are minutes after this job starts.</p></div><span className="plan-check-status">{rows.length} tasks</span></div>
    <dl className="plan-task-counts"><div><dt>Finished</dt><dd>{finished}</dd></div><div><dt>Pending</dt><dd>{rows.length - finished - blocked}</dd></div><div><dt>Needs attention</dt><dd>{blocked}</dd></div></dl>
    {rows.length === 0 ? <p className="plan-check-note">No individual pickups in this plan. Area cleaning progress is shown on the Route map.</p> : <ol className="plan-task-list">{rows.map(row => <li className="plan-check-card" key={row.id}>
      <div className="plan-check-heading"><h3>{row.label}</h3><span className={`plan-check-status ${row.done ? "is-good" : row.blocked ? "is-bad" : ""}`}>{row.done ? "Finished" : row.blocked ? "Not included" : row.schedule ? "Scheduled" : "Pending"}</span></div>
      <dl className="plan-task-facts"><div><dt>Priority</dt><dd>{finite(row.task?.priority) ? `${row.task.priority} / 10` : "Not recorded"}</dd></div><div><dt>Service time</dt><dd>{seconds(row.task?.service_duration_sec)}</dd></div><div><dt>Allowed time</dt><dd>{finite(row.task?.time_window_start_min) && finite(row.task?.time_window_end_min) ? `${time(row.task.time_window_start_min)} to ${time(row.task.time_window_end_min)}` : "Not recorded"}</dd></div><div><dt>Planned service</dt><dd>{row.schedule && finite(row.schedule.service_start_min) && finite(row.schedule.service_end_min) ? `${time(row.schedule.service_start_min)} to ${time(row.schedule.service_end_min)}` : "Not scheduled"}</dd></div></dl>
      {row.blocked && <p className="plan-task-warning">{reasonMessage(row.reasons)}</p>}
      {row.schedule?.disposal_before && <p className="plan-check-note">A disposal stop is planned before this pickup.</p>}
      <details><summary>Task details</summary><p>Task reference: <code>{row.id}</code></p>{row.reasons.length > 0 && <ul>{row.reasons.map((reason, index) => <li key={index}><code>{reason}</code></li>)}</ul>}{Boolean(row.skip?.recommended_actions?.length) && <ul>{row.skip!.recommended_actions!.map((action, index) => <li key={index}>{action}</li>)}</ul>}</details>
    </li>)}</ol>}
    <p className="plan-check-note">Higher numbers mean higher recorded priority.</p>
  </section>;
}
