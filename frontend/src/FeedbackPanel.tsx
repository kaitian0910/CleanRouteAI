import React, { useEffect, useRef, useState } from "react";
import type { AuditEntry, ExecutionState, RouteCandidate, RouteHistorySummary, TelemetryFrame } from "./types/api";
import { canRepeatRoute } from "./repeatRun";
import { PlanChecks, TaskReview } from "./PlanChecks";
import "./FeedbackPanel.css";

export interface PolicyStatus {
  mode: "TRAINED_PPO" | "DETERMINISTIC_FALLBACK";
  checkpoint_loaded: boolean;
  checkpoint: string;
  observation_schema: string;
  action_schema: string;
  detail: string;
  training_enabled: false;
}

export interface CandidateFeedback {
  route_id: string;
  route_version: number;
  source: string;
  observation: number[];
  proposed_action: {
    action_index: number;
    task_id: string | null;
    route_mode: "DIRECT";
    preferred_task_id: string | null;
  } | null;
  reward: number;
  reward_components: Record<string, number>;
  feasible: boolean;
  failed_constraints: string[];
  decision: string;
  reason: string;
  metrics: Record<string, number>;
  metric_deltas: Record<string, number>;
  inference_ms: number;
}

export interface RouteFeedback {
  policy: PolicyStatus;
  candidates: CandidateFeedback[];
  execution_feedback: Record<string, unknown>[];
  state?: string;
}

export interface RouteLogs {
  execution_state: string;
  audit_log: AuditEntry[];
  history_versions: RouteHistorySummary[];
  candidate_options?: RouteHistorySummary[];
  technical_audit_log?: AuditEntry[];
}

export interface DailyMissionRecord {
  record_id: string;
  job_id?: string;
  route_id?: string;
  recorded_at?: string;
  machine_id?: string;
  layout_fingerprint?: string;
  mission_elapsed_minutes?: number;
  estimated_completion_minutes?: number;
  actual_completion_minutes?: number | null;
  coverage_percent?: number;
  outing_count?: number;
  depot_visits?: number;
  charging_duration_minutes?: number;
  selected_sweep_angle_deg?: number;
  selected_overlap_ratio?: number;
  status?: string;
}

export interface DailyMissionHistory {
  records: DailyMissionRecord[];
  detail?: string;
}

const number = (value: number) => Number.isFinite(value)
  ? value.toLocaleString(undefined, { maximumFractionDigits: 3 }) : "Unavailable";
const label = (value: string) => value.replace(/_/g, " ");

function routeOutcome(status: string): string {
  switch (status) {
    case "COMPLETED": return "Finished";
    case "EXECUTING": return "Used for this run";
    case "APPROVED": return "Approved to run";
    case "PROPOSED": return "Ready for review";
    case "DRAFT_FAILED_KPI": return "Needs attention";
    case "SUPERSEDED": return "Replaced by a newer plan";
    case "ABORTED": return "Stopped";
    default: return label(status);
  }
}

function candidateOutcome(candidate: CandidateFeedback, route: RouteCandidate): string {
  if (!candidate.feasible) return "Not usable: failed checks";
  if (candidate.route_id === route.route_id) {
    if (route.status === "COMPLETED") return "Used for this completed run";
    if (route.status === "EXECUTING") return "Used for this run";
    if (route.status === "APPROVED") return "Chosen and approved";
    if (route.status === "SUPERSEDED") return "Chosen for an earlier plan";
    if (route.status === "ABORTED") return "Chosen for this stopped run";
    return "Chosen for review";
  }
  return "Not chosen";
}

function NumberFields({ values, signed = false }: { values: Record<string, number>; signed?: boolean }) {
  return <dl className="feedback-number-fields">
    {Object.entries(values).map(([key, value]) => <div key={key}>
      <dt>{label(key)}</dt><dd>{signed && value > 0 ? "+" : ""}{number(value)}</dd>
    </div>)}
  </dl>;
}

export function PolicyCard({ policy }: { policy: PolicyStatus | null }) {
  const loaded = policy?.mode === "TRAINED_PPO" && policy.checkpoint_loaded;
  return <section className="feedback-card feedback-policy" aria-label="Reinforcement learning policy status">
    <div className="feedback-card-heading"><h2>Reinforcement learning policy</h2>
      <span className={`feedback-badge ${loaded ? "good" : "warning"}`}>
        {policy ? loaded ? "Trained PPO inference" : "Deterministic fallback" : "Status unavailable"}
      </span>
    </div>
    <p>{policy?.detail || "Checking the backend for a compatible policy checkpoint."}</p>
    {policy && <dl className="feedback-policy-fields">
      <div><dt>Checkpoint loaded</dt><dd>{policy.checkpoint_loaded ? "Yes" : "No"}</dd></div>
      <div><dt>Checkpoint</dt><dd>{policy.checkpoint || "None configured"}</dd></div>
      <div><dt>Observation schema</dt><dd>{policy.observation_schema}</dd></div>
      <div><dt>Permitted action schema</dt><dd>{policy.action_schema}</dd></div>
    </dl>}
    <p className="feedback-note">Runtime uses frozen inference. Evaluation feedback is recorded for replanning; model weights are not updated during missions. All execution is simulated.</p>
    <p className="feedback-note">The supplied PPO was trained for six-bin dispatch. It cannot learn new sweep patterns or daily area layouts during a mission. Daily route changes are evaluated by the planner; training a policy for sweep order, depot decisions and completion time requires a matching training environment and offline retraining.</p>
  </section>;
}

export function TechnicalFeedbackView({ policy, feedback, route }: {
  policy: PolicyStatus | null; feedback: RouteFeedback | null; route: RouteCandidate | null;
}) {
  return <>
    <PolicyCard policy={policy} />
    <section className="feedback-card">
      <div className="feedback-card-heading"><h2>Planning feedback loop</h2>
        {route && <span className="feedback-badge">{label(route.status)}</span>}
      </div>
      <p className="feedback-loop">Observe <span>→</span> Propose settings <span>→</span> Plan <span>→</span> Evaluate <span>→</span> Revise or await approval</p>
      <p className="feedback-note">The deterministic planner creates each route. The independent evaluator gates feasibility before the existing approval workflow.</p>
      {route ? <p><strong>Replanning trigger: </strong>{route.replan_reason || "Initial mission plan"}<br />
        <span className="feedback-note">{route.route_id} · plan {route.route_version} · {label(route.routing_provenance)}</span></p>
        : <p className="feedback-empty">Plan a mission from the Route view to inspect its recommendations and evaluation feedback.</p>}
    </section>
    {route?.depot_cycles_enabled && <section className="feedback-card" aria-label="Whole-job completion feedback">
      <h2>Whole-job completion</h2>
      <div className="feedback-candidate-summary">
        <span>Completion including depot service <strong>{number(route.estimated_completion_minutes ?? route.estimated_duration_minutes)} min</strong></span>
        <span>Outings <strong>{route.outing_count ?? 1}</strong></span>
        <span>Recharge stops <strong>{route.depot_visits ?? 0}</strong></span>
        <span>Depot service <strong>{number(route.charging_duration_minutes ?? 0)} min</strong></span>
        <span>Longest outing <strong>{number(route.max_outing_duration_minutes ?? 0)} min</strong></span>
        <span>Lowest planned battery <strong>{number(route.minimum_battery_percent ?? route.estimated_remaining_battery_percent)}%</strong></span>
      </div>
      <p className="feedback-note">Completion is elapsed mission time at estimated finish, including remaining work, return travel and simulated recharge time. Recharges use the machine's configured depot service duration; the simulator advances each service in one step. The per-outing battery reserve and time limits still apply. Candidate comparisons do not prove that a globally optimal daily route was found.</p>
    </section>}
    {route && <section aria-label="Route options checked">
      <div className="feedback-section-heading"><h2>Route options checked</h2><span>Planning estimates · {feedback?.candidates.length ?? 0} checked</span></div>
      <p className="feedback-note">These are alternatives checked during this planning attempt. Only the chosen option becomes the plan for this run. Checking an option does not mean the machine drove it or the reinforcement learning model trained on it.</p>
      {!feedback?.candidates.length && <p className="feedback-card feedback-empty">No option checks have been recorded for this planning attempt yet.</p>}
      {feedback?.candidates.map((candidate, index) => <article className="feedback-card" key={`${candidate.route_id}-${index}`}>
        <div className="feedback-card-heading"><h3>Option {index + 1}</h3>
          <span className={`feedback-badge ${!candidate.feasible ? "bad" : candidate.route_id === route.route_id ? "good" : ""}`}>{candidateOutcome(candidate, route)}</span>
        </div>
        <p>{candidate.reason}</p>
        <div className="feedback-candidate-summary">
          <span>Estimated completion <strong>{friendlyDuration(candidate.metrics.estimated_completion_minutes ?? candidate.metrics.estimated_duration_minutes)}</strong></span>
          <span>Planning checks <strong>{candidate.feasible ? "Passed" : "Failed"}</strong></span>
          <span>Suggested by <strong>{candidate.source === "TRAINED_PPO" ? "Reinforcement learning bin model" : "Coverage and collection planner"}</strong></span>
        </div>
        {!!candidate.failed_constraints.length && <div className="feedback-failures" role="note"><strong>Failed hard constraints</strong><ul>
          {candidate.failed_constraints.map(constraint => <li key={constraint}>{label(constraint)}</li>)}
        </ul></div>}
        <details className="feedback-option-details"><summary>Technical details for this option</summary>
        <p className="feedback-note">Internal option ID: {candidate.route_id}; plan revision: {candidate.route_version}. Recorded planning decision: {candidate.decision}. This records the selection at planning time; the current run status is shown above.</p>
        <div className="feedback-candidate-summary">
          <span>Source <strong>{label(candidate.source)}</strong></span>
          <span>Feasible <strong>{candidate.feasible ? "Yes" : "No"}</strong></span>
          <span>CleanRoute AI KPI reward <strong>{number(candidate.reward)}</strong></span>
          <span>Inference <strong>{number(candidate.inference_ms)} ms</strong></span>
          {candidate.proposed_action && <>
            <span>Policy action <strong>{candidate.proposed_action.action_index}</strong></span>
            <span>Next collection task <strong>{candidate.proposed_action.preferred_task_id || candidate.proposed_action.task_id || "Return to depot"}</strong></span>
            <span>Route mode <strong>{candidate.proposed_action.route_mode}</strong></span>
          </>}
        </div>
        <div className="feedback-candidate-columns">
          <section><h4>Evaluator metrics</h4><NumberFields values={candidate.metrics} /></section>
          <section><h4>CleanRoute AI KPI reward components</h4><NumberFields values={candidate.reward_components} /></section>
        </div>
        {!!Object.keys(candidate.metric_deltas).length && <section><h4>Change from baseline candidate</h4>
          <p className="feedback-note">Proposed minus baseline. Positive values describe increases, not necessarily improvements.</p>
          <NumberFields values={candidate.metric_deltas} signed />
        </section>}
        <details><summary>Numerical policy observation ({candidate.observation.length} values)</summary>
          <p className="feedback-observation">[{candidate.observation.map(number).join(", ")}]</p>
        </details>
        </details>
      </article>)}
    </section>}
    {route && <section className="feedback-card"><h2>Simulated execution feedback</h2>
      <p className="feedback-note">Candidate rewards above are calculated by the CleanRoute AI evaluator and are separate from the checkpoint's original G3 training reward.</p>
      {!feedback?.execution_feedback.length ? <p className="feedback-empty">No simulated execution feedback recorded yet.</p>
        : feedback.execution_feedback.map((entry, index) => <details key={index}>
          <summary>Execution record {index + 1}</summary><pre>{JSON.stringify(entry, null, 2)}</pre>
        </details>)}
    </section>}
  </>;
}

export function friendlyDuration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "Not recorded";
  const minutes = Math.max(0, Math.round(value));
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}h${minutes % 60 ? ` ${minutes % 60}min` : ""}` : `${minutes}min`;
}

const wholeNumber = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? "—" : Math.round(value).toLocaleString();
const hasNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const isCompletedRunRecord = (status?: string) => status === "COMPLETED" || status === "COMPLETED_SIMULATION";

function plainExclusionReason(value: string): string {
  if (/water|lake|river/i.test(value)) return "Water";
  if (/building/i.test(value)) return "Buildings";
  if (/road|crossing/i.test(value)) return "Roads without permitted access";
  if (/unverified/i.test(value)) return "Areas awaiting access checks";
  if (/isolated|unreachable|connect/i.test(value)) return "Areas without a safe connection";
  if (/obstacle|restricted|exclusion/i.test(value)) return "Obstacles or restricted areas";
  return "Areas the machine cannot safely enter";
}

interface FeedbackViewProps {
  policy: PolicyStatus | null;
  feedback: RouteFeedback | null;
  route: RouteCandidate | null;
  history?: DailyMissionHistory | null;
  onRunAgain?: (route: RouteCandidate) => void;
  runAgainBusy?: boolean;
  onOpenRoute?: () => void;
  showTechnical?: boolean;
  executionState?: ExecutionState;
  telemetry?: TelemetryFrame | null;
}

export function FeedbackView({ policy, feedback, route, history, onRunAgain, runAgainBusy = false, onOpenRoute, showTechnical = true, executionState, telemetry }: FeedbackViewProps) {
  // The step API rests in PAUSED between simulated segments. The controller's
  // state distinguishes that scheduling detail from an operator pressing Pause.
  const state = executionState ?? (route?.status === "COMPLETED" ? "COMPLETED" : feedback?.state ?? route?.status);
  const completed = state === "COMPLETED";
  const running = state === "EXECUTING";
  const paused = state === "PAUSED";
  const emergencyStop = state === "EMERGENCY_STOP";
  const needsAttention = state === "NEEDS_ASSISTANCE";
  const currentTelemetry = telemetry?.route_id === route?.route_id ? telemetry : null;
  const repeatable = completed && canRepeatRoute(route);
  const segments = route?.segments ?? [];
  const returnedToDepot = route?.depot_cycles_enabled || segments[segments.length - 1]?.segment_type === "RETURN_TO_BASE";
  const blocked = route?.status === "DRAFT_FAILED_KPI";
  const executionRecord = feedback?.execution_feedback[feedback.execution_feedback.length - 1];
  const savedRecord = history?.records.find(record => record.route_id === route?.route_id && isCompletedRunRecord(record.status));
  const recordedDuration = currentTelemetry?.mission_elapsed_min ?? (hasNumber(executionRecord?.mission_elapsed_min) ? executionRecord.mission_elapsed_min : savedRecord?.actual_completion_minutes);
  const totalDuration = completed && hasNumber(recordedDuration) ? recordedDuration : completed ? route?.estimated_duration_minutes : route?.estimated_completion_minutes ?? route?.estimated_duration_minutes;
  const selectedDuration = completed ? route?.estimated_duration_minutes : route?.estimated_completion_minutes ?? route?.estimated_duration_minutes;
  const hasLiveBattery = currentTelemetry != null || hasNumber(executionRecord?.battery_percent);
  const battery = currentTelemetry?.battery_percent ?? (hasNumber(executionRecord?.battery_percent) ? executionRecord.battery_percent : route?.depot_cycles_enabled ? route.minimum_battery_percent : route?.estimated_remaining_battery_percent);
  const completedSegments = segments.filter(segment => segment.completed).length;
  const collectionTasks = route?.tasks?.filter(task => ["RUBBISH_COLLECTION", "OVERSIZED_RUBBISH"].includes(task.task_type)) ?? [];
  const completedCollections = collectionTasks.filter(task => task.completed).length;
  const excludedArea = (route?.inaccessible_area_m2 ?? 0) + (route?.unverified_area_m2 ?? 0);
  const exclusionReasons = [...new Set((route?.planning_diagnostics?.excluded_sections ?? route?.unreachable_sections ?? []).map(section => plainExclusionReason(section.reason)))];
  if (route?.exclusion_zones?.length && !exclusionReasons.includes("Obstacles or restricted areas")) exclusionReasons.push("Obstacles or restricted areas");
  const candidates = feedback?.candidates ?? [];
  const failedChecks = route?.kpi_evaluation?.failed_constraints ?? [];
  const attentionReasons = [...new Set(failedChecks.map(reason => /time_window|deadline/.test(reason) ? "Some pickups cannot finish by their deadlines."
    : /energy|battery/.test(reason) ? "A depot return with enough battery reserve could not be planned."
    : /duration/.test(reason) ? "An outing takes longer than the allowed time."
    : /travel_distance/.test(reason) ? "An outing exceeds the travel distance limit."
    : /unreachable|geometry|connectivity/.test(reason) ? "Some work has no confirmed safe access path."
    : "Some required planning checks still need attention."))];
  const firstPlan = candidates[0];
  const firstDuration = firstPlan?.metrics.estimated_completion_minutes ?? firstPlan?.metrics.estimated_duration_minutes;
  const selectedCandidate = candidates.find(candidate => candidate.route_id === route?.route_id);
  const selectedPpo = selectedCandidate?.source === "TRAINED_PPO" && selectedCandidate.feasible
    && selectedCandidate.decision === "SELECTED_AWAITING_APPROVAL";
  const checkedPpo = candidates.some(candidate => candidate.source === "TRAINED_PPO");
  const areaPlan = !!route?.operating_boundary || ["AREA_SWEEP", "HYBRID", "PATROL"].includes(route?.task_mode ?? "");
  const plannerExplanation = areaPlan
    ? "Coverage planner selected. Your supplied reinforcement learning model orders bin visits for six-bin rounds. It does not choose this area's sweeping path or learn a new path during a run."
    : selectedPpo
      ? "Reinforcement learning bin order selected after checking access, timing and battery limits. The supplied model makes suggestions; its training stays unchanged during a run."
      : checkedPpo
        ? blocked ? "The reinforcement learning bin order was checked, but this plan did not pass all required checks. Review the issues on the map before running. The model's training stays unchanged during a run."
          : "The reinforcement learning bin order was checked. The standard plan was kept because the suggestion did not improve the validated result. The model's training stays unchanged during a run."
        : policy
          ? "Standard bin planner selected. The supplied reinforcement learning model was not used for this round; it needs a compatible six-bin collection round."
          : "Checking the recorded planning method. Model details are available below when the backend responds.";
  const title = completed ? "This run is finished" : emergencyStop ? "Emergency stop is engaged" : blocked || needsAttention ? "This plan needs attention" : running ? "Cleaning is in progress" : paused ? "Cleaning is paused" : state === "ABORTED" ? "This run was stopped" : state === "PLANNING" || state === "REPLANNING" ? "Checking a new route" : route ? "Your route is ready to review" : "Start with an area to clean";
  return <>
    <section className={`feedback-card feedback-outcome ${completed ? "is-complete" : ""}`} aria-label="Cleaning result and next step">
      <div className="feedback-card-heading"><div><span className="feedback-eyebrow">{completed ? "Completed run" : "Your cleaning plan"}</span><h2>{title}</h2></div>
        {route && <span className="feedback-badge">{route.routing_provenance === "SIMULATED" ? "Offline simulation" : "Simulation"}</span>}
      </div>
      {!route ? <><p>Choose an area on the Route map, select the cleaning task, then review and run the plan.</p>
        {onOpenRoute && <button className="btn-primary-sm" type="button" onClick={onOpenRoute}>Open Route map</button>}</>
        : <>
          <p>{completed ? repeatable ? `${returnedToDepot ? "The reachable work is finished and the machine has returned to the depot." : "The reachable work is finished."} Start a fresh run to clean this area again.` : route.routing_provenance === "SIMULATED" ? "This offline preview is finished. Use the Route map to plan a new run." : "The reported items have been collected. Add a new waste observation or collection point for more work." : emergencyStop ? "Movement is stopped. Open the Route map to review the stopped job before starting another simulation." : blocked ? "Review the marked access limits or collection deadlines on the map before running." : needsAttention ? "Open the Route map to check the issue before continuing. Your completed work is saved." : running ? "The machine follows the route, returns for depot service when needed, and continues with unfinished work." : paused ? "Your completed work is saved. Open the Route map to resume when ready." : "Review the route on the map, then approve it to start cleaning."}</p>
          {(running || paused || completed) && segments.length > 0 && <p className="feedback-live-progress" role="status">Route progress: <strong>{wholeNumber(completedSegments / segments.length * 100)}%</strong> <span>({completedSegments} of {segments.length} steps finished)</span>{currentTelemetry && <> · {friendlyDuration(currentTelemetry.mission_elapsed_min)} elapsed</>}</p>}
          {blocked && !!attentionReasons.length && <ul className="feedback-attention-reasons">{attentionReasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
          <dl className="feedback-human-metrics">
            <div><dt>{completed && hasNumber(recordedDuration) ? "Recorded completion time" : "Estimated total time"}</dt><dd>{friendlyDuration(totalDuration)}</dd><small>Including travel and depot service</small></div>
            <div><dt>{completed ? "Area cleaned" : "Area to clean"}</dt><dd>{wholeNumber(route.covered_area_m2)} m²</dd><small>{wholeNumber(route.effective_eligible_area_m2 ?? route.target_area_m2)} m² reachable</small></div>
            <div><dt>Collections finished</dt><dd>{completedCollections} / {collectionTasks.length}</dd><small>Bins and individual items</small></div>
            <div><dt>{hasLiveBattery ? "Latest battery" : "Lowest planned battery"}</dt><dd>{wholeNumber(battery)}%</dd><small>Reserve required: {wholeNumber(route.kpi_policy_snapshot?.min_energy_reserve_percent ?? 20)}%</small></div>
            <div><dt>Recharge stops</dt><dd>{route.depot_visits ?? 0}</dd><small>{friendlyDuration(route.charging_duration_minutes ?? 0)} at the depot</small></div>
          </dl>
          {(excludedArea > 0 || exclusionReasons.length > 0) && <p className="feedback-area-note"><strong>{wholeNumber(route.requested_area_m2 ?? route.target_area_m2)} m² selected · {wholeNumber(excludedArea)} m² left out.</strong>{" "}{exclusionReasons.length ? exclusionReasons.join("; ") : "Some areas do not have safe machine access"}. This excluded area is outside the cleaning target.</p>}
          <div className="feedback-next-actions">
            {repeatable && onRunAgain && <button className="btn-primary-sm feedback-run-again" type="button" disabled={runAgainBusy} onClick={() => onRunAgain(route)}>{runAgainBusy ? "Preparing a fresh run…" : "Run this area again"}</button>}
            {onOpenRoute && <button className="btn-secondary-sm" type="button" onClick={onOpenRoute}>Open Route map</button>}
          </div>
          {repeatable && onRunAgain && <p className="feedback-note">Starts a new simulated job with the same area, bin locations and obstacles. Your previous results are kept.</p>}
        </>}
    </section>
    {route?.repeat_comparison && <section className="feedback-card feedback-repeat-comparison" aria-label="New run compared with the previous run">
      <div className="feedback-card-heading"><h2>This run was planned again</h2><span className="feedback-badge">{route.repeat_comparison.candidates_checked} options checked · {route.repeat_comparison.feasible_candidates} passed</span></div>
      <p>{route.repeat_comparison.summary}</p>
      <dl className="feedback-repeat-decisions">
        {areaPlan && <div><dt>Sweeping pattern</dt><dd>{route.repeat_comparison.sweep_pattern_changed ? "Updated" : "Kept"}</dd></div>}
        {collectionTasks.length > 0 && <div><dt>Bin visit order</dt><dd>{route.repeat_comparison.collection_order_changed ? "Updated" : "Kept"}</dd></div>}
        <div><dt>Travel path</dt><dd>{route.repeat_comparison.travel_path_changed ? "Updated" : "Kept"}</dd></div>
        <div><dt>Saved planning history</dt><dd>{route.repeat_comparison.history_applied ? "Rechecked" : "No matching result used"}</dd></div>
      </dl>
      <div className="feedback-time-comparison">
        <span>Previous plan estimate<strong>{friendlyDuration(route.repeat_comparison.previous_estimated_completion_minutes)}</strong>Including depot service</span>
        <span>New plan estimate<strong>{friendlyDuration(route.repeat_comparison.new_estimated_completion_minutes)}</strong>Including depot service</span>
      </div>
      <p className="feedback-note">{Math.abs(route.repeat_comparison.estimated_time_change_minutes) < 0.5 ? "The estimated time is about the same." : `The new estimate is ${friendlyDuration(Math.abs(route.repeat_comparison.estimated_time_change_minutes))} ${route.repeat_comparison.estimated_time_change_minutes < 0 ? "shorter" : "longer"}.`}{hasNumber(route.repeat_comparison.previous_actual_completion_minutes) ? ` The previous simulation recorded ${friendlyDuration(route.repeat_comparison.previous_actual_completion_minutes)}.` : ""}{completed ? hasNumber(recordedDuration) ? ` This simulation recorded ${friendlyDuration(recordedDuration)}.` : " This simulation's recorded time is unavailable." : " The new simulation's recorded time is available after it finishes."}</p>
    </section>}
    {route && <section className="feedback-card feedback-route-choice"><h2>How the route was chosen</h2>
      <p>{collectionTasks.length ? route.operating_boundary ? "Collection points are scheduled around their deadlines, then the machine covers the remaining cleaning area." : "Collection points are scheduled around their deadlines and safe access paths." : "The planner covers the reachable cleaning area and avoids marked obstacles."} {route.depot_cycles_enabled ? "Depot returns split the work into manageable trips." : "This plan uses one continuous outing."}</p>
      {candidates.length > 1 && hasNumber(firstDuration) && hasNumber(selectedDuration) ? <div className="feedback-time-comparison">
        <span>First route checked <strong>{friendlyDuration(firstDuration)}</strong>{firstPlan.feasible ? "" : "Needed changes"}</span>
        <span>Selected route <strong>{friendlyDuration(selectedDuration)}</strong>Includes depot service</span>
      </div> : <p className="feedback-note">{candidates.length > 1 ? `${candidates.length} route options were checked.` : "A safe route is checked against access, collection deadlines and the machine's limits."} A time comparison appears when more than one timed option is recorded.</p>}
      <p className="feedback-planner-method">{plannerExplanation}</p>
      <p className="feedback-note">Running the area again recalculates the plan. When the area, obstacles and machine limits stay the same, the best checked path can stay the same too. Changing the drawing alone does not make the route faster.</p>
    </section>}
    {showTechnical && <details className="feedback-technical"><summary>Technical details</summary><TechnicalFeedbackView policy={policy} feedback={feedback} route={route} /></details>}
  </>;
}

function displayTime(timestamp: string) {
  const value = new Date(timestamp);
  return Number.isNaN(value.getTime()) ? timestamp : value.toLocaleString();
}

export function DailyMissionHistoryView({ history }: { history: DailyMissionHistory | null }) {
  const records = [...(history?.records ?? [])].sort((a, b) => (b.recorded_at ?? "").localeCompare(a.recorded_at ?? ""));
  const duration = (value: number | null | undefined) => value == null ? "Not recorded" : `${number(value)} min`;
  const seenJobs = new Set<string>();
  const completedRuns = records.filter(record => {
    if (!isCompletedRunRecord(record.status)) return false;
    const key = record.job_id ?? record.route_id ?? record.record_id;
    if (seenJobs.has(key)) return false;
    seenJobs.add(key); return true;
  }).slice(0, 5);
  return <section className="feedback-card" aria-label="Daily mission comparison">
    <div className="feedback-card-heading"><h2>Recent completed runs</h2><span className="feedback-note">Saved simulation results</span></div>
    {completedRuns.length ? <ul className="feedback-recent-runs">{completedRuns.map(record => <li key={record.record_id}>
      <div><strong>{record.recorded_at ? friendlyRunDate(record.recorded_at) : "Completed run"}</strong><span>{record.machine_id ?? "Machine"} · {record.coverage_percent == null ? "Coverage not recorded" : `${wholeNumber(record.coverage_percent)}% of reachable area`}</span></div>
      <div><strong>{friendlyDuration(record.actual_completion_minutes ?? record.estimated_completion_minutes)}</strong><span>{record.actual_completion_minutes == null ? "Estimated time" : "Recorded time"} · {record.depot_visits ?? 0} recharge stops</span></div>
    </li>)}</ul> : <p className="feedback-empty">Your completed runs will appear here, with their finish time and recharge stops.</p>}
    <p className="feedback-note">Different areas or changed conditions can need different amounts of work. A shorter run alone does not show a better route.</p>
    <details className="feedback-history-details"><summary>Technical details: all saved run records</summary>
    <div className="feedback-card-heading"><h3>Daily mission comparison</h3><span className="feedback-badge">Latest {Math.min(records.length, 20)} records</span></div>
    <p className="feedback-note">Recorded plans and simulated execution outcomes persist across backend restarts. Compare completion estimates with recorded completion time. Changed layout or conditions may contain different work, so a faster time alone does not establish improvement. Feedback does not train model weights online.</p>
    {history?.detail && <p className="feedback-note">{history.detail}</p>}
    {!records.length ? <p className="feedback-empty">No saved mission records yet. Plan and execute a mission to record its timing and depot visits.</p>
      : <div className="feedback-table-wrap"><table className="feedback-table"><thead><tr>
        <th>Recorded / machine</th><th>Mission / status</th><th>Layout</th><th>Planned completion</th><th>Recorded completion</th><th>Elapsed</th><th>Outings / recharges</th><th>Depot service</th><th>Coverage</th><th>Sweep settings</th>
      </tr></thead><tbody>{records.slice(0, 20).map((record, index) => {
        const previous = records.slice(index + 1).find(item => item.machine_id === record.machine_id && item.layout_fingerprint);
        const layout = !record.layout_fingerprint || !record.machine_id || !previous
          ? "No prior comparable layout" : record.layout_fingerprint === previous.layout_fingerprint ? "Same layout and settings" : "Layout or conditions changed";
        return <tr key={record.record_id}>
          <td>{record.recorded_at ? displayTime(record.recorded_at) : "Not recorded"}<br />{record.machine_id ?? "Unknown machine"}</td>
          <td>{record.job_id ?? record.route_id ?? record.record_id}<br />{record.status ? label(record.status) : "Not recorded"}</td>
          <td>{layout}</td><td>{duration(record.estimated_completion_minutes)}</td><td>{duration(record.actual_completion_minutes)}</td>
          <td>{duration(record.mission_elapsed_minutes)}</td>
          <td>{record.outing_count ?? "—"} / {record.depot_visits ?? "—"}</td>
          <td>{duration(record.charging_duration_minutes)}</td>
          <td>{record.coverage_percent == null ? "Not recorded" : `${number(record.coverage_percent)}%`}</td>
          <td>{record.selected_sweep_angle_deg == null ? "—" : `${number(record.selected_sweep_angle_deg)}°`}, {record.selected_overlap_ratio == null ? "—" : `${number(record.selected_overlap_ratio * 100)}% overlap`}</td>
        </tr>;
      })}</tbody></table></div>}
    </details>
  </section>;
}

function friendlyRunDate(timestamp: string) {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? "Date not recorded" : new Intl.DateTimeFormat("en-MY", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kuala_Lumpur" }).format(date);
}

function revisionReason(reason?: string | null): string {
  const reasons: Record<string, string> = {
    operator_revision_preserving_progress: "Area or task settings changed; finished work was kept.",
    operating_boundary_changed: "The cleaning area changed.",
    obstacle_report_with_validated_exclusion: "A new obstacle needed a safe detour.",
    new_validated_waste_location: "A new waste collection point was added.",
    missed_collection: "A missed pickup needed another visit.",
    task_time_window_changed: "A pickup deadline changed.",
    high_route_deviation: "The machine moved away from its planned path.",
    low_battery_remaining_mission: "The remaining work was replanned for the current battery.",
  };
  return reason ? reasons[reason] ?? label(reason) : "First plan for this cleaning run.";
}

export function PlanningDecisionSummary({ route }: { route: RouteCandidate | null }) {
  const repeat = route?.repeat_comparison;
  const revision = route?.replan_comparison;
  const failed = route?.kpi_evaluation?.passed === false || route?.status === "DRAFT_FAILED_KPI"
    || !!route?.route_safety_issues?.length;
  const pathsChanged = repeat && (repeat.sweep_pattern_changed || repeat.collection_order_changed || repeat.travel_path_changed);
  const badge = !route ? "No plan yet" : failed ? "Needs attention" : repeat ? pathsChanged ? "Plan updated" : "Path kept"
    : revision || route.replan_reason ? "Plan revised" : routeOutcome(route.status);
  const title = !route ? "Choose an area to begin" : failed ? "The plan needs attention before it can run"
    : repeat ? "The area was planned again" : revision ? "The remaining work was planned again"
    : route.status === "COMPLETED" ? "Plan used for this completed run" : "The cleaning plan was checked";
  return <section className={`feedback-card feedback-decision ${failed ? "needs-attention" : ""}`} aria-label="Latest planning decision">
    <div className="feedback-card-heading"><div><span className="feedback-eyebrow">Latest planning decision</span><h2>{title}</h2></div>
      <span className={`feedback-badge ${failed ? "bad" : route?.kpi_evaluation?.passed ? "good" : ""}`}>{badge}</span>
    </div>
    {!route ? <p>The next plan will check the cleaning area, collection points, battery and depot returns.</p>
      : failed ? <><p>One or more required planning checks did not pass. Review Plan checks before starting or resuming this job.</p>
        {!!route.kpi_evaluation?.failed_constraints?.length && <p className="feedback-note">Recorded checks: {route.kpi_evaluation.failed_constraints.map(label).join(", ")}.</p>}</>
      : repeat ? <><p>{repeat.summary}</p><dl className="feedback-decision-facts">
        <div><dt>Options checked</dt><dd>{repeat.candidates_checked}</dd></div>
        <div><dt>Options passing checks</dt><dd>{repeat.feasible_candidates}</dd></div>
        <div><dt>Previous plan estimate</dt><dd>{friendlyDuration(repeat.previous_estimated_completion_minutes)}</dd></div>
        <div><dt>New plan estimate</dt><dd>{friendlyDuration(repeat.new_estimated_completion_minutes)}</dd></div>
      </dl><p className="feedback-note">{pathsChanged ? "At least one part of the sweeping, bin visit order or travel path changed." : "The checked plan kept the same sweeping pattern, bin order and travel path."} These are planning estimates; comparing options does not retrain the model.</p></>
      : revision ? <><p>{revisionReason(route.replan_reason)}</p><dl className="feedback-decision-facts">
        <div><dt>Previous remaining estimate</dt><dd>{friendlyDuration(revision.previous_remaining_duration_min)}</dd></div>
        <div><dt>Revised remaining estimate</dt><dd>{friendlyDuration(revision.revised_remaining_duration_min)}</dd></div>
        <div><dt>Remaining distance</dt><dd>{wholeNumber(revision.revised_remaining_distance_m)} m</dd></div>
      </dl><p className="feedback-note">These figures compare the remaining work at replanning time, rather than two whole cleaning runs.</p></>
      : <p>{revisionReason(route.replan_reason)} {route.status === "COMPLETED" ? "The run is finished; a repeat creates a new job." : route.status === "PROPOSED" ? "Review the checks and approve it when you are ready." : "The recorded plan is shown below."}</p>}
    {route && <div className="feedback-decision-footer"><span>Plan {route.route_version}</span>{route.created_at && <time dateTime={route.created_at}>{displayTime(route.created_at)}</time>}</div>}
  </section>;
}

function activityTitle(action: string): string {
  const titles: Record<string, string> = {
    PLAN_SELECTED: "Cleaning plan checked", RL_SELECTION: "Cleaning plan checked",
    RL_RECOMMENDATION: "Bin visit suggestion checked", APPROVE_ROUTE: "Plan approved",
    REPEAT_COMPLETED_JOB: "Fresh cleaning run created", DEPOT_RECHARGE: "Depot service finished",
    COMPARISON_STOPPED: "Route comparison finished",
    PAUSE_JOB: "Cleaning paused", EMERGENCY_STOP: "Emergency stop recorded",
    COMPLETE_JOB: "Cleaning finished", CANCEL_JOB: "Cleaning job cancelled",
    "EVENT:OBSTACLE_DETECTED": "Obstacle added", "EVENT:OVERSIZED_RUBBISH": "Waste collection added",
    "EVENT:TIME_WINDOW_CHANGED": "Pickup deadline changed", "EVENT:MISSED_COLLECTION": "Missed pickup reported",
    "EVENT:LOW_BATTERY": "Low battery reported", "EVENT:EMERGENCY_STOP": "Emergency stop engaged",
    "EVENT:SENSOR_FAULT": "Sensor fault reported", "EVENT:FULL_PAYLOAD": "Waste hopper reported full",
  };
  return titles[action] ?? label(action.replace(/^EVENT:/, ""));
}

function activityAppearance(action: string): { tone: string; badge: string } {
  if (action === "EMERGENCY_STOP" || action === "EVENT:EMERGENCY_STOP") return { tone: "critical", badge: "Safety stop" };
  if (["EVENT:SENSOR_FAULT", "EVENT:LOW_BATTERY", "EVENT:FULL_PAYLOAD", "EVENT:MISSED_COLLECTION"].includes(action)) return { tone: "attention", badge: "Attention" };
  if (action === "EVENT:OBSTACLE_DETECTED") return { tone: "attention", badge: "Obstacle update" };
  if (action === "COMPLETE_JOB") return { tone: "success", badge: "Finished" };
  if (action === "DEPOT_RECHARGE") return { tone: "success", badge: "Depot service" };
  if (action === "APPROVE_ROUTE") return { tone: "success", badge: "Approved" };
  if (action === "PAUSE_JOB") return { tone: "attention", badge: "Paused" };
  if (action === "CANCEL_JOB") return { tone: "attention", badge: "Cancelled" };
  if (["PLAN_SELECTED", "RL_SELECTION"].includes(action)) return { tone: "info", badge: "Plan decision" };
  if (action === "RL_RECOMMENDATION") return { tone: "info", badge: "Suggestion checked" };
  if (action === "COMPARISON_STOPPED") return { tone: "info", badge: "Comparison ended" };
  if (action === "REPEAT_COMPLETED_JOB") return { tone: "info", badge: "New run" };
  return { tone: "info", badge: "Recorded event" };
}

type RevisionSummary = RouteHistorySummary & {
  planning_run_id?: string | null; candidate_index?: number; is_selected_candidate?: boolean;
  reason?: string; options_checked?: number; created_at?: string; approved_by?: string | null; executed?: boolean;
};

function OptionTable({ options }: { options: RevisionSummary[] }) {
  return <div className="feedback-table-wrap"><table className="feedback-table"><thead><tr>
    <th>Option</th><th>Selection</th><th>Planning checks</th><th>Coverage</th><th>Distance</th><th>Estimated time</th><th>Failed checks</th>
  </tr></thead><tbody>{options.map((item, index) => <tr key={item.route_id}>
    <td>{item.candidate_index ?? index + 1}</td><td>{item.is_selected_candidate ? "Chosen" : "Not chosen"}</td>
    <td>{item.passed ? "Passed" : "Failed"}</td><td>{item.coverage_percent == null ? "N/A" : `${number(item.coverage_percent)}%`}</td>
    <td>{number(item.total_distance_m)} m</td><td>{friendlyDuration(item.estimated_duration_minutes)}</td>
    <td>{item.failed_constraints.length ? item.failed_constraints.map(label).join(", ") : "None"}</td>
  </tr>)}</tbody></table></div>;
}

export function LogsView({ logs, route, executionState }: { logs: RouteLogs | null; route: RouteCandidate | null; executionState?: ExecutionState }) {
  const revisions: RevisionSummary[] = logs?.history_versions ?? [];
  const options: RevisionSummary[] = logs?.candidate_options ?? [];
  const rawAudit = logs?.technical_audit_log ?? logs?.audit_log ?? [];
  const operatorEvents = (logs?.audit_log ?? []).filter(entry => !entry.action.startsWith("STATE:") && !["RL_CANDIDATE", "CANDIDATE_EVALUATED"].includes(entry.action));
  const state = executionState ?? logs?.execution_state;
  const stateLabel = state === "COMPLETED" ? "Finished" : state === "EXECUTING" ? "Cleaning" : state === "PAUSED" ? "Paused" : state === "AWAITING_APPROVAL" ? "Ready for review" : state ? label(state) : null;
  const planDecisionCount = operatorEvents.filter(entry => ["PLAN_SELECTED", "RL_SELECTION"].includes(entry.action)).length;
  const depotServiceCount = operatorEvents.filter(entry => entry.action === "DEPOT_RECHARGE").length;
  const safetyAlertCount = operatorEvents.filter(entry => ["EMERGENCY_STOP", "EVENT:EMERGENCY_STOP", "EVENT:SENSOR_FAULT", "EVENT:LOW_BATTERY", "EVENT:FULL_PAYLOAD"].includes(entry.action)).length;
  return <>
    <section className="feedback-card"><div className="feedback-card-heading"><h2>Changes to this cleaning plan</h2>
      {stateLabel && <span className="feedback-badge">{stateLabel}</span>}
    </div>
      <p>Each plan below is the result of one planning request. The planner may compare several route options before choosing one. Those options are not extra cleaning runs.</p>
      {!route ? <p className="feedback-empty">Select or plan a mission to view its route history and audit log.</p>
        : !revisions.length ? <p className="feedback-empty">No route history is available yet.</p>
        : <ol className="feedback-revision-list">{[...revisions].reverse().map(item => {
          const matchingOptions = options.filter(option => item.planning_run_id ? option.planning_run_id === item.planning_run_id : option.version === item.version);
          const optionCount = item.options_checked ?? (matchingOptions.length || 1);
          return <li key={item.route_id}>
            <div className="feedback-card-heading"><h3>Plan {item.version}</h3><span className={`feedback-badge ${item.passed ? "good" : "bad"}`}>{routeOutcome(item.status)}</span></div>
            <p>{revisionReason(item.reason)}</p>
            <div className="feedback-revision-summary">
              <span>{optionCount} route {optionCount === 1 ? "option" : "options"} checked</span>
              <span>Estimated time: <strong>{friendlyDuration(item.estimated_duration_minutes)}</strong></span>
              <span>{item.passed ? "Planning checks passed" : "Planning checks failed"}</span>
              <span>{item.executed ? "Movement recorded in simulation" : item.approved_by ? "Approved; no movement recorded" : "No movement recorded"}</span>
              {item.created_at && <time dateTime={item.created_at}>{displayTime(item.created_at)}</time>}
            </div>
            {item.failed_constraints.length > 0 && <p className="feedback-failures">Checks needing attention: {item.failed_constraints.map(label).join(", ")}</p>}
            {matchingOptions.length > 0 && <details className="feedback-plan-options"><summary>Options compared for this plan ({matchingOptions.length})</summary>
              <p className="feedback-note">These estimates came from the same planning request. Only the chosen option became this plan. They do not represent training episodes.</p>
              <OptionTable options={matchingOptions} />
            </details>}
          </li>;
        })}</ol>}
      <p className="feedback-note">This page covers the current cleaning run. Feedback shows previous completed runs. All movement and collection shown here are simulated.</p>
    </section>
    <section className="feedback-card feedback-activity"><div className="feedback-card-heading"><h2>Activity for this run</h2>
      <span className="feedback-note">{operatorEvents.length} recent {operatorEvents.length === 1 ? "event" : "events"}</span></div>
      {!!operatorEvents.length && <><dl className="feedback-activity-counts" aria-label="Counts in recent activity">
        <div><dt>Plan decisions</dt><dd>{planDecisionCount}</dd></div>
        <div><dt>Depot services</dt><dd>{depotServiceCount}</dd></div>
        <div><dt>Safety alerts</dt><dd>{safetyAlertCount}</dd></div>
      </dl><p className="feedback-note">Counts cover the recent events loaded below, rather than lifetime totals. An alert being recorded does not mean it has been resolved.</p></>}
      {!operatorEvents.length ? <p className="feedback-empty">No recorded events for this mission.</p>
        : <ol className="feedback-audit-list feedback-timeline">{[...operatorEvents].reverse().map(entry => {
          const appearance = activityAppearance(entry.action);
          return <li key={entry.audit_id} className={`feedback-event ${appearance.tone}`}>
          <div className="feedback-event-time"><time dateTime={entry.timestamp}>{displayTime(entry.timestamp)}</time>{entry.route_version != null && <span>Plan {entry.route_version}</span>}</div>
          <div className="feedback-card-heading"><strong>{activityTitle(entry.action)}</strong><span className={`feedback-event-badge ${appearance.tone}`}>{appearance.badge}</span></div>
          <p>{entry.action === "PLAN_SELECTED" || entry.action === "RL_SELECTION" ? "The planner finished checking route options and chose the plan recorded above. Approval and execution are separate steps." : entry.reason}</p>
          <div className="feedback-note">Recorded by {entry.actor}</div>
        </li>; })}</ol>}
    </section>
    <details className="feedback-technical feedback-raw-audit"><summary>Technical audit log ({rawAudit.length} recent events)</summary>
      <p className="feedback-note">Raw events include internal planning checks and state transitions between simulated steps. They are not additional plan revisions or completed runs.</p>
      {!rawAudit.length ? <p className="feedback-empty">No technical events recorded.</p> : <ol className="feedback-audit-list">{[...rawAudit].reverse().map(entry => <li key={entry.audit_id}>
        <div className="feedback-card-heading"><strong>{entry.action}</strong><time dateTime={entry.timestamp}>{displayTime(entry.timestamp)}</time></div>
        <p>{entry.reason}</p><div className="feedback-note">{entry.actor}{entry.route_version != null ? ` · plan ${entry.route_version}` : ""}{entry.route_id ? ` · ${entry.route_id}` : ""}</div>
        {entry.details && !!Object.keys(entry.details).length && <details><summary>Recorded details</summary><pre>{JSON.stringify(entry.details, null, 2)}</pre></details>}
      </li>)}</ol>}
      {options.length > 0 && <details><summary>All checked option identifiers</summary><pre>{JSON.stringify(options, null, 2)}</pre></details>}
    </details>
  </>;
}

interface Props {
  apiBase: string;
  token: string;
  route: RouteCandidate | null;
  view: "FEEDBACK" | "LOGS";
  onRunAgain?: (route: RouteCandidate) => void;
  runAgainBusy?: boolean;
  onOpenRoute?: () => void;
  executionState?: ExecutionState;
  telemetry?: TelemetryFrame | null;
}

export default function FeedbackPanel({ apiBase, token, route: activeRoute, view, onRunAgain, runAgainBusy, onOpenRoute, executionState, telemetry }: Props) {
  const [policy, setPolicy] = useState<PolicyStatus | null>(null);
  const [feedback, setFeedback] = useState<RouteFeedback | null>(null);
  const [logs, setLogs] = useState<RouteLogs | null>(null);
  const [dailyHistory, setDailyHistory] = useState<DailyMissionHistory | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [demoRoute, setDemoRoute] = useState<RouteCandidate | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [demoError, setDemoError] = useState<string | null>(null);
  const [section, setSection] = useState<"SUMMARY" | "CHECKS" | "TASKS">("SUMMARY");
  const demoController = useRef<AbortController | null>(null);
  const route = demoRoute || activeRoute;
  const routeId = route?.route_id;
  const version = route?.route_version;
  const routeStatus = route?.status;
  const historyMachineId = activeRoute?.machine_id;
  const taskCount = new Set([...(activeRoute?.tasks ?? []).map(task => task.task_id),
    ...(activeRoute?.skipped_tasks ?? []).map(task => task.task_id)]).size;
  const simulatedOffline = route?.routing_provenance === "SIMULATED";

  useEffect(() => { setDemoRoute(null); }, [activeRoute?.route_id]);
  useEffect(() => { setSection("SUMMARY"); }, [view, activeRoute?.job_id]);
  useEffect(() => () => { demoController.current?.abort(); }, []);
  useEffect(() => {
    if ((executionState ?? routeStatus) !== "EXECUTING") return;
    const interval = window.setInterval(() => setRefresh(value => value + 1), 10000);
    return () => window.clearInterval(interval);
  }, [routeStatus, executionState]);

  async function runDemo(useRl: boolean) {
    setDemoBusy(true);
    setDemoError(null);
    const controller = new AbortController();
    demoController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 60000);
    try {
      const response = await fetch(`${apiBase}/api/rl/demo`, {
        method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ use_rl: useRl }),
        signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Demo failed (${response.status})`);
      setDemoRoute(data);
      setRefresh(value => value + 1);
    } catch (error) {
      setDemoError(controller.signal.aborted ? "Demo request timed out or was cancelled. Refresh to inspect available records." : error instanceof Error ? error.message : "Demo unavailable.");
    } finally { window.clearTimeout(timeout); setDemoBusy(false); }
  }

  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    setBusy(true);
    setErrors([]);
    setFeedback(null);
    setLogs(null);
    setDailyHistory(null);
    setUpdatedAt(null);
    if (view === "FEEDBACK") setPolicy(null);
    async function request(path: string) {
      const response = await fetch(`${apiBase}${path}`, {
        headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
      return data;
    }
    const policyRequest = view === "FEEDBACK" ? request("/api/rl/status") : Promise.resolve(null);
    const routeRequest = routeId && !simulatedOffline
      ? request(`/api/routes/${encodeURIComponent(routeId)}/${view === "FEEDBACK" ? "feedback" : "metrics"}`) : Promise.resolve(null);
    const historyRequest = view === "FEEDBACK"
      ? request(`/api/feedback/history${historyMachineId ? `?machine_id=${encodeURIComponent(historyMachineId)}` : ""}`) : Promise.resolve(null);
    Promise.allSettled([policyRequest, routeRequest, historyRequest]).then(results => {
      if (!current) return;
      const [policyResult, routeResult, historyResult] = results;
      if (historyResult.status === "fulfilled" && historyResult.value) setDailyHistory(historyResult.value);
      if (policyResult.status === "fulfilled" && policyResult.value) setPolicy(policyResult.value);
      if (routeResult.status === "fulfilled" && routeResult.value) {
        if (view === "FEEDBACK") { setFeedback(routeResult.value); setPolicy(routeResult.value.policy); }
        else setLogs(routeResult.value);
      }
      setErrors(results.flatMap(result => result.status === "rejected"
        ? [controller.signal.aborted ? "Backend request timed out. Refresh to retry." : result.reason?.message || "Backend unavailable."] : []));
      setUpdatedAt(new Date().toLocaleTimeString());
      setBusy(false);
      window.clearTimeout(timeout);
    });
    return () => { current = false; controller.abort(); window.clearTimeout(timeout); };
  }, [apiBase, token, routeId, version, routeStatus, historyMachineId, simulatedOffline, view, refresh]);

  return <div className="feedback-panel">
    <div className="feedback-page-heading"><div><h1>{view === "FEEDBACK" ? "Feedback" : "Logs & changes"}</h1>
      <p>{view === "FEEDBACK" ? "Your cleaning results, finish times and next run." : "Recorded events and changes to your route."}</p>
    </div><div className="feedback-refresh"><span role="status">{busy ? "Loading records…" : updatedAt ? `Checked ${updatedAt}` : ""}</span>
      <button className="btn-secondary-sm" type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>Refresh</button>
    </div></div>
    {!!errors.length && <div className="feedback-error" role="alert">{[...new Set(errors)].join(" · ")}</div>}
    {view === "FEEDBACK" && <div className="feedback-view-tabs" role="tablist" aria-label="Cleaning plan information">
      {([ ["SUMMARY", "Summary"], ["CHECKS", "Plan checks"], ["TASKS", `Tasks (${taskCount})`] ] as const).map(([key, title]) =>
        <button key={key} type="button" role="tab" id={`feedback-tab-${key.toLowerCase()}`}
          aria-selected={section === key} aria-controls={`feedback-section-${key.toLowerCase()}`}
          tabIndex={section === key ? 0 : -1} onClick={() => setSection(key)}
          onKeyDown={event => {
            const keys = ["SUMMARY", "CHECKS", "TASKS"] as const;
            const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
            if (direction || event.key === "Home" || event.key === "End") {
              event.preventDefault();
              const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (keys.indexOf(key) + direction + 3) % 3;
              setSection(keys[next]);
              document.getElementById(`feedback-tab-${keys[next].toLowerCase()}`)?.focus();
            }
          }}>{title}</button>)}
    </div>}
    {view === "FEEDBACK" && section === "CHECKS" && <div role="tabpanel" id="feedback-section-checks" aria-labelledby="feedback-tab-checks">
      <PlanningDecisionSummary route={activeRoute} />
      <PlanChecks route={activeRoute} telemetry={telemetry ?? null} onOpenRoute={onOpenRoute} />
    </div>}
    {view === "FEEDBACK" && section === "TASKS" && <div role="tabpanel" id="feedback-section-tasks" aria-labelledby="feedback-tab-tasks">
      <TaskReview route={activeRoute} />
    </div>}
    {view === "FEEDBACK" && section === "SUMMARY" && <div role="tabpanel" id="feedback-section-summary" aria-labelledby="feedback-tab-summary">
      <FeedbackView policy={demoRoute ? null : policy} feedback={demoRoute ? null : feedback} route={activeRoute}
      history={dailyHistory} onRunAgain={onRunAgain} runAgainBusy={runAgainBusy} onOpenRoute={onOpenRoute} showTechnical={!demoRoute} executionState={executionState} telemetry={telemetry} />
      <DailyMissionHistoryView history={dailyHistory} />
    <details className="feedback-technical feedback-examples"><summary>Examples</summary>
    {demoError && <div className="feedback-error" role="alert">{demoError}</div>}
    {demoRoute && <div className="feedback-demo-banner"><span>Showing a separate six-bin example. Your current cleaning run is unchanged.</span>
      <button type="button" className="btn-secondary-sm" onClick={() => setDemoRoute(null)}>Return to your cleaning results</button>
    </div>}
    <section className="feedback-card"><div className="feedback-card-heading"><h2>Six-bin policy demonstration</h2>
      <div className="feedback-demo-actions"><button type="button" className="btn-primary-sm" disabled={demoBusy} onClick={() => void runDemo(true)}>Run six-bin RL demo</button>
        <button type="button" className="btn-secondary-sm" disabled={demoBusy} onClick={() => void runDemo(false)}>Run baseline demo</button></div>
    </div><p className="feedback-note">Uses the checkpoint's six-bin observation contract with the CleanRoute AI planner and evaluator. Demonstration proposals remain subject to independent feasibility checks. No weights are trained.</p>
      {demoBusy && <p role="status">Planning and evaluating the demonstration…</p>}
    </section>
    {demoRoute && <TechnicalFeedbackView policy={policy} feedback={feedback} route={demoRoute} />}
    </details>
    </div>}
    {view === "LOGS" && <LogsView logs={logs} route={activeRoute} executionState={executionState} />}
    {activeRoute?.routing_provenance === "SIMULATED" && <p className="feedback-note">This is an offline preview. Saved backend results are unavailable for this run.</p>}
  </div>;
}
