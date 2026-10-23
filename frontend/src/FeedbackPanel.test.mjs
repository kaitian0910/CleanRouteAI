import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const source = readFileSync(new URL('./FeedbackPanel.tsx', import.meta.url), 'utf8')
  .replace('import "./FeedbackPanel.css";', '');
const { outputText } = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
  jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
} });
const exports = {};
const { outputText: repeatHelperText } = ts.transpileModule(readFileSync(new URL('./repeatRun.ts', import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } });
const repeatHelperExports = {};
new Function('require', 'exports', repeatHelperText)(createRequire(import.meta.url), repeatHelperExports);
const require = createRequire(import.meta.url);
const { outputText: planChecksText } = ts.transpileModule(readFileSync(new URL('./PlanChecks.tsx', import.meta.url), 'utf8')
  .replace('import "./PlanChecks.css";', ''), { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } });
const planChecksExports = {};
new Function('require', 'exports', planChecksText)(require, planChecksExports);
new Function('require', 'exports', outputText)(name => name === './repeatRun' ? repeatHelperExports : name === './PlanChecks' ? planChecksExports : require(name), exports);
const { FeedbackView, TechnicalFeedbackView, PolicyCard, LogsView, DailyMissionHistoryView, friendlyDuration } = exports;
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const policy = {
  mode: 'DETERMINISTIC_FALLBACK', checkpoint_loaded: true, checkpoint: 'ppo_model.zip',
  observation_schema: 'g3-bin-dispatch-v1', action_schema: 'discrete-14',
  detail: 'Mission lacks the six-bin state required by the checkpoint.', training_enabled: false,
};

test('feedback displays mission fallback even if a checkpoint was loaded globally', () => {
  const html = render(PolicyCard, { policy });
  assert.match(html, /Deterministic fallback/);
  assert.match(html, /Mission lacks the six-bin state/);
  assert.match(html, /weights are not updated/);
  assert.doesNotMatch(html, /Trained PPO inference/);
  assert.match(render(PolicyCard, { policy: { ...policy, mode: 'TRAINED_PPO' } }), /Trained PPO inference/);
});

test('high reward never hides rejected candidate hard constraints', () => {
  const candidate = {
    route_id: 'route-test', route_version: 2, source: 'TRAINED_PPO', observation: [0, 0.5, 1],
    proposed_action: { action_index: 0, task_id: 'bin-1', preferred_task_id: 'bin-1', route_mode: 'DIRECT' },
    reward: 999, reward_components: { coverage: 1, duration_penalty: -0.1 }, feasible: false,
    failed_constraints: ['energy_reserve_below_minimum'], decision: 'REJECTED',
    reason: 'Battery reserve failed the independent KPI gate.', metrics: { coverage_percent: 100 },
    metric_deltas: { total_distance_m: 0, estimated_duration_minutes: -3 }, inference_ms: 1.25,
  };
  const html = render(FeedbackView, { policy, feedback: { policy, candidates: [candidate], execution_feedback: [] },
    route: { route_id: 'route-test', route_version: 2, status: 'REJECTED', routing_provenance: 'PLANNED' } });
  assert.match(html, /REJECTED/);
  assert.match(html, /energy reserve below minimum/);
  assert.match(html, /999/);
  assert.match(html, /Next collection task/);
  assert.match(html, /bin-1/);
  assert.match(html, /<dt>total distance m<\/dt><dd>0<\/dd>/);
  assert.match(html, /<dd>-3<\/dd>/);
});

test('no-route feedback and logs show clear empty states without fabricated results', () => {
  const feedback = render(FeedbackView, { policy, feedback: null, route: null });
  assert.match(feedback, /Plan a mission/);
  assert.doesNotMatch(feedback, /Candidate 1/);
  assert.match(render(LogsView, { logs: null, route: null }), /Select or plan a mission/);
});

test('multi-outing feedback includes depot service in whole-job completion', () => {
  const html = render(FeedbackView, { policy, feedback: null, route: {
    route_id: 'depot-cycle', route_version: 1, status: 'PROPOSED', routing_provenance: 'PLANNED',
    depot_cycles_enabled: true, outing_count: 4, depot_visits: 3, charging_duration_minutes: 60,
    max_outing_duration_minutes: 80, minimum_battery_percent: 25, estimated_completion_minutes: 360,
  } });
  assert.match(html, /Whole-job completion/);
  assert.match(html, /360 min/);
  assert.match(html, /Recharge stops/);
  assert.match(html, /60 min/);
  assert.match(html, /six-bin dispatch/);
  assert.match(html, /weights are not updated/);
});

test('logs render recorded version outcome and audit reason', () => {
  const html = render(LogsView, { route: { route_id: 'route-1' }, logs: {
    execution_state: 'AWAITING_APPROVAL', history_versions: [{ route_id: 'route-1', version: 1,
      status: 'REJECTED', passed: false, coverage_percent: null, total_distance_m: 123,
      estimated_duration_minutes: 2, failed_constraints: ['time_window_violation'] }],
    audit_log: [{ audit_id: 'audit-1', timestamp: '2026-10-23T00:00:00Z', actor: 'SYSTEM',
      action: 'RL_SELECTION', reason: 'Baseline retained after failed policy candidate.', route_version: 1, details: {} }],
  } });
  assert.match(html, /time window violation/);
  assert.match(html, /Baseline retained after failed policy candidate/);
  assert.match(html, /Cleaning plan checked/);
  assert.match(html, /RL_SELECTION/);
  assert.match(html, /No movement recorded/);
});

test('saved daily comparison distinguishes estimates, simulation completion and changed layouts', () => {
  const history = { records: [
    { record_id: 'one', machine_id: 'alpha', recorded_at: '2026-10-20T01:00:00Z', layout_fingerprint: 'A', estimated_completion_minutes: 160, actual_completion_minutes: 165 },
    { record_id: 'two', machine_id: 'alpha', recorded_at: '2026-10-21T01:00:00Z', layout_fingerprint: 'A', estimated_completion_minutes: 155, actual_completion_minutes: null },
    { record_id: 'three', machine_id: 'alpha', recorded_at: '2026-10-22T01:00:00Z', layout_fingerprint: 'B', outing_count: 3, depot_visits: 2, charging_duration_minutes: 40, coverage_percent: 99.5 },
    { record_id: 'four', machine_id: 'beta', recorded_at: '2026-10-23T01:00:00Z', layout_fingerprint: 'B' },
  ] };
  const html = render(DailyMissionHistoryView, { history });
  assert.match(html, /Planned completion/);
  assert.match(html, /Recorded completion/);
  assert.match(html, /165 min/);
  assert.match(html, /Same layout and settings/);
  assert.match(html, /Layout or conditions changed/);
  assert.match(html, /No prior comparable layout/);
  assert.match(html, /3 \/ 2/);
  assert.match(html, /40 min/);
  assert.match(html, /does not establish improvement/);
  assert.match(html, /does not train model weights online/);
  assert.ok(html.indexOf('four') < html.indexOf('three'));
});

test('daily history limits display to twenty records and supports missing values', () => {
  const history = { records: Array.from({ length: 25 }, (_, index) => ({ record_id: `saved-${index}` })) };
  const html = render(DailyMissionHistoryView, { history });
  assert.match(html, /Latest 20 records/);
  assert.doesNotMatch(html, /saved-24/);
  assert.match(html, /Not recorded/);
  assert.match(render(DailyMissionHistoryView, { history: null }), /No saved mission records yet/);
});

const completedRoute = {
  route_id: 'completed-real-area', job_id: 'job-real', route_version: 2, status: 'COMPLETED',
  routing_provenance: 'PLANNED', task_mode: 'HYBRID', operating_boundary: { vertices: [] },
  estimated_duration_minutes: 125, estimated_completion_minutes: 0,
  requested_area_m2: 10000, effective_eligible_area_m2: 9400, covered_area_m2: 9350,
  inaccessible_area_m2: 600, unverified_area_m2: 0, depot_cycles_enabled: true,
  outing_count: 3, depot_visits: 2, charging_duration_minutes: 40, minimum_battery_percent: 25,
  estimated_remaining_battery_percent: 30, kpi_policy_snapshot: { min_energy_reserve_percent: 20 },
  tasks: [{ task_id: 'bin-1', task_type: 'RUBBISH_COLLECTION', completed: true }],
  unreachable_sections: [{ reason: 'water_body_exclusion', area_m2: 600 }], exclusion_zones: [],
};

test('default feedback leads with a readable result and repeat action, with diagnostics closed', () => {
  const html = render(FeedbackView, { route: completedRoute, policy,
    feedback: { candidates: [], execution_feedback: [{ mission_elapsed_min: 97.23, battery_percent: 30 }] },
    onRunAgain: () => {}, onOpenRoute: () => {},
  });
  const visibleSummary = html.split('<details class="feedback-technical">')[0];
  assert.match(visibleSummary, /This run is finished/);
  assert.match(visibleSummary, /Recorded completion time/);
  assert.match(visibleSummary, /1h 37min/);
  assert.match(visibleSummary, /Run this area again/);
  assert.match(visibleSummary, /Your previous results are kept/);
  assert.match(visibleSummary, /600 m² left out/);
  assert.match(visibleSummary, /Water/);
  assert.match(visibleSummary, /Latest battery/);
  assert.match(visibleSummary, /30%/);
  assert.doesNotMatch(visibleSummary, /observation schema|checkpoint|reward|KPI|654\.244|ppo_model/i);
  assert.match(html, /<details class="feedback-technical"><summary>Technical details<\/summary>/);
  assert.doesNotMatch(html, /<details[^>]*class="feedback-technical"[^>]* open/);
});

test('repeat action requires a completed active run and a handler, and disables while preparing', () => {
  assert.doesNotMatch(render(FeedbackView, { route: completedRoute, policy, feedback: null }), /Run this area again/);
  assert.doesNotMatch(render(FeedbackView, { route: { ...completedRoute, status: 'PROPOSED' }, policy, feedback: null, onRunAgain: () => {} }), /Run this area again/);
  const busy = render(FeedbackView, { route: completedRoute, policy, feedback: null, onRunAgain: () => {}, runAgainBusy: true });
  assert.match(busy, /disabled=""[^>]*>Preparing a fresh run/);
  assert.match(busy.split('<details class="feedback-technical">')[0], /2h 5min/, 'completed fallback uses original total rather than refreshed zero remaining estimate');
});

test('recent completed runs use compact dates and durations while raw history stays collapsed', () => {
  const html = render(DailyMissionHistoryView, { history: { records: [
    { record_id: 'finished', job_id: 'one', status: 'COMPLETED', recorded_at: '2026-10-23T01:00:00Z', actual_completion_minutes: 97.4, depot_visits: 2, coverage_percent: 99.42 },
    { record_id: 'pending', job_id: 'two', status: 'PROPOSED', recorded_at: '2026-10-24T01:00:00Z', estimated_completion_minutes: 70 },
  ] } });
  const visibleSummary = html.split('<details class="feedback-history-details">')[0];
  assert.match(visibleSummary, /Recent completed runs/);
  assert.match(visibleSummary, /23 Oct 2026/);
  assert.match(visibleSummary, /1h 37min/);
  assert.match(visibleSummary, /2 recharge stops/);
  assert.doesNotMatch(visibleSummary, /24 Oct|<table|layout_fingerprint|99\.42/);
  assert.match(html, /<details class="feedback-history-details"><summary>Technical details: all saved run records/);
});

test('friendly durations avoid decimal-minute noise and preserve unavailable values', () => {
  assert.equal(friendlyDuration(97.2344), '1h 37min');
  assert.equal(friendlyDuration(120), '2h');
  assert.equal(friendlyDuration(9.7), '10min');
  assert.equal(friendlyDuration(undefined), 'Not recorded');
});

test('completed one-off litter jobs require a fresh observation and cannot repeat collected objects', () => {
  const html = render(FeedbackView, { policy, feedback: null, onRunAgain: () => {}, route: {
    ...completedRoute, operating_boundary: null,
    tasks: [{ task_id: 'litter', task_type: 'RUBBISH_COLLECTION', completed: true, waste_observation: { waste_class: 'Bottle' } }],
  } });
  const visibleSummary = html.split('<details class="feedback-technical">')[0];
  assert.doesNotMatch(visibleSummary, /Run this area again/);
  assert.match(visibleSummary, /Add a new waste observation/);
});

test('backend completed simulation history supplies recorded finish time and recent-run entry', () => {
  const history = { records: [{ record_id: 'saved', route_id: completedRoute.route_id, job_id: 'saved-job', status: 'COMPLETED_SIMULATION', actual_completion_minutes: 97, recorded_at: '2026-10-23T01:00:00Z' }] };
  const summary = render(FeedbackView, { route: completedRoute, policy, feedback: null, history }).split('<details class="feedback-technical">')[0];
  assert.match(summary, /Recorded completion time/);
  assert.match(summary, /1h 37min/);
  const recent = render(DailyMissionHistoryView, { history }).split('<details class="feedback-history-details">')[0];
  assert.match(recent, /23 Oct 2026/);
  assert.match(recent, /1h 37min/);
});

test('completion wording does not claim a depot return for a route that ends elsewhere', () => {
  const summary = render(FeedbackView, { route: { ...completedRoute, depot_cycles_enabled: false, segments: [{ segment_type: 'SWEEP_SWATH' }] }, policy, feedback: null }).split('<details class="feedback-technical">')[0];
  assert.match(summary, /The reachable work is finished/);
  assert.doesNotMatch(summary, /has returned to the depot/);
});

test('running controller overrides the backend pause between segments and uses fresh telemetry', () => {
  const route = { ...completedRoute, status: 'EXECUTING', segments: [{ completed: true }, { completed: false }] };
  const html = render(FeedbackView, { route, policy, executionState: 'EXECUTING',
    telemetry: { route_id: route.route_id, mission_elapsed_min: 15.1, battery_percent: 82 },
    feedback: { state: 'PAUSED', candidates: [], execution_feedback: [{ battery_percent: 95 }] },
  }).split('<details class="feedback-technical">')[0];
  assert.match(html, /Cleaning is in progress/);
  assert.doesNotMatch(html, /Cleaning is paused|resume when ready/);
  assert.match(html, /Route progress: <strong>50%/);
  assert.match(html, /1 of 2 steps finished/);
  assert.match(html, /15min elapsed/);
  assert.match(html, /82%/);
  assert.doesNotMatch(html, />95%/);
});

test('manual pause and emergency stop remain visible even when the route says executing', () => {
  const route = { ...completedRoute, status: 'EXECUTING' };
  const props = { route, policy, feedback: { state: 'EXECUTING', candidates: [], execution_feedback: [] } };
  const paused = render(FeedbackView, { ...props, executionState: 'PAUSED' }).split('<details class="feedback-technical">')[0];
  assert.match(paused, /Cleaning is paused/);
  assert.match(paused, /resume when ready/);
  assert.doesNotMatch(paused, /Cleaning is in progress/);
  const stopped = render(FeedbackView, { ...props, executionState: 'EMERGENCY_STOP' }).split('<details class="feedback-technical">')[0];
  assert.match(stopped, /Emergency stop is engaged/);
  assert.match(stopped, /review the stopped job before starting another simulation/);
});

test('telemetry from another run cannot overwrite this run battery or finish time', () => {
  const html = render(FeedbackView, { route: completedRoute, policy, feedback: null,
    telemetry: { route_id: 'another-route', mission_elapsed_min: 999, battery_percent: 1 },
  }).split('<details class="feedback-technical">')[0];
  assert.match(html, /2h 5min/);
  assert.match(html, /Lowest planned battery/);
  assert.match(html, /25%/);
  assert.doesNotMatch(html, /16h 39min|>1%/);
});

test('default planning explanation distinguishes coverage, selected PPO and rejected PPO suggestions', () => {
  const area = render(FeedbackView, { route: completedRoute, policy, feedback: null }).split('<details class="feedback-technical">')[0];
  assert.match(area, /Coverage planner selected/);
  assert.match(area, /does not choose this area&#x27;s sweeping path/);
  assert.match(area, /best checked path can stay the same/);
  assert.doesNotMatch(area, /learned assistant is available/);
  const candidate = { route_id: 'bin-route', source: 'TRAINED_PPO', feasible: true, decision: 'SELECTED_AWAITING_APPROVAL', metrics: {} };
  const route = { ...completedRoute, route_id: 'bin-route', operating_boundary: null, task_mode: 'RUBBISH_COLLECTION' };
  const selected = render(FeedbackView, { route, policy, showTechnical: false,
    feedback: { candidates: [candidate], execution_feedback: [] },
  });
  assert.match(selected, /Reinforcement learning bin order selected/);
  const rejected = render(FeedbackView, { route, policy: { ...policy, mode: 'TRAINED_PPO' }, showTechnical: false,
    feedback: { candidates: [{ ...candidate, route_id: 'rejected-route', decision: 'REJECTED_NO_IMPROVEMENT' }], execution_feedback: [] },
  });
  assert.match(rejected, /standard plan was kept/);
  assert.doesNotMatch(rejected, /Reinforcement learning bin order selected/);
});

test('repeat comparison reports checked paths and distinguishes estimates from prior recorded time', () => {
  const html = render(FeedbackView, { route: { ...completedRoute, status: 'EXECUTING', repeat_comparison: {
    previous_route_id: 'prior-run', candidates_checked: 5, feasible_candidates: 4,
    previous_estimated_completion_minutes: 125, previous_actual_completion_minutes: 127,
    new_estimated_completion_minutes: 123, estimated_time_change_minutes: -2,
    sweep_pattern_changed: false, collection_order_changed: true, travel_path_changed: true,
    history_applied: true, summary: 'The previous sweeping pattern remained the quickest safe option checked.',
  } }, policy, feedback: null, showTechnical: false });
  assert.match(html, /This run was planned again/);
  assert.match(html, /5 options checked/);
  assert.match(html, /Sweeping pattern<\/dt><dd>Kept/);
  assert.match(html, /Bin visit order<\/dt><dd>Updated/);
  assert.match(html, /Previous plan estimate/);
  assert.match(html, /New plan estimate/);
  assert.match(html, /new estimate is 2min shorter/);
  assert.match(html, /previous simulation recorded 2h 7min/);
  assert.match(html, /recorded time is available after it finishes/);
});

test('options explain one planning attempt and selected badges follow the current run lifecycle', () => {
  const selected = {
    route_id: completedRoute.route_id, route_version: 1, source: 'DETERMINISTIC_FALLBACK',
    observation: [], proposed_action: null, reward: 1, reward_components: {}, feasible: true,
    failed_constraints: [], decision: 'SELECTED_AWAITING_APPROVAL', reason: 'Independent planning checks passed.',
    metrics: { estimated_completion_minutes: 125 }, metric_deltas: {}, inference_ms: 0,
  };
  const alternative = { ...selected, route_id: 'other-option', decision: 'NOT_SELECTED' };
  const props = { policy, route: completedRoute,
    feedback: { candidates: [selected, alternative], execution_feedback: [] },
  };
  const html = render(TechnicalFeedbackView, props);
  assert.match(html, /Route options checked/);
  assert.match(html, /alternatives checked during this planning attempt/);
  assert.match(html, /does not mean the machine drove it/);
  assert.match(html, /<h3>Option 1<\/h3>/);
  assert.match(html, /Used for this completed run/);
  assert.match(html, /Not chosen/);
  assert.doesNotMatch(html, /<h3>Candidate|<h3>Option 1.*version|feedback-badge[^>]*>SELECTED/);
  assert.match(html, /<details class="feedback-option-details"><summary>Technical details for this option/);
  assert.match(html, /Recorded planning decision: SELECTED_AWAITING_APPROVAL/);
  assert.doesNotMatch(html, /<details[^>]*class="feedback-option-details"[^>]* open/);
  assert.match(render(TechnicalFeedbackView, { ...props, route: { ...completedRoute, status: 'EXECUTING' } }), /Used for this run/);
  assert.match(render(TechnicalFeedbackView, { ...props, route: { ...completedRoute, status: 'PROPOSED' } }), /Chosen for review/);
});

test('logs show two published plan revisions, with six checked alternatives kept collapsed per revision', () => {
  const option = (version, index) => ({ route_id: `plan-${version}-option-${index}`, version,
    planning_run_id: `planning-${version}`, candidate_index: index, is_selected_candidate: index === 2,
    status: index === 2 ? version === 2 ? 'COMPLETED' : 'SUPERSEDED' : 'PROPOSED',
    passed: true, score: 1, coverage_percent: 99, total_distance_m: 1000,
    estimated_duration_minutes: 100 + index, failed_constraints: [],
  });
  const options = [1, 2].flatMap(version => [1, 2, 3, 4, 5, 6].map(index => option(version, index)));
  const revisions = [1, 2].map(version => ({ ...option(version, 2), options_checked: 6,
    reason: version === 2 ? 'obstacle_report_with_validated_exclusion' : null,
    executed: true, approved_by: 'operator',
  }));
  const selection = { audit_id: 'selection', timestamp: '2026-10-23T00:00:00Z', actor: 'planner',
    action: 'PLAN_SELECTED', route_version: 2, reason: 'Selected option 2', details: {} };
  const state = { ...selection, audit_id: 'state', action: 'STATE:EXECUTING->PAUSED', reason: 'Safe pause at segment boundary' };
  const internal = { ...selection, audit_id: 'candidate', action: 'CANDIDATE_EVALUATED', reason: 'Candidate score 1' };
  const html = render(LogsView, { route: completedRoute, logs: {
    execution_state: 'COMPLETED', history_versions: revisions, candidate_options: options,
    audit_log: [selection], technical_audit_log: [selection, state, internal],
  } });
  const operator = html.split('<details class="feedback-technical feedback-raw-audit">')[0];
  assert.equal((operator.match(/<h3>Plan \d<\/h3>/g) ?? []).length, 2);
  assert.match(operator, /Those options are not extra cleaning runs/);
  assert.match(operator, /A new obstacle needed a safe detour/);
  assert.match(operator, /6 route options checked/);
  assert.match(operator, /Movement recorded in simulation/);
  assert.match(operator, /<details class="feedback-plan-options"><summary>Options compared for this plan \(6\)/);
  assert.doesNotMatch(operator, /STATE:|Safe pause at segment boundary|CANDIDATE_EVALUATED|Plan 3/);
  assert.match(html, /Technical audit log \(3 recent events\)/);
  assert.match(html, /STATE:EXECUTING-&gt;PAUSED/);
  assert.match(html, /All checked option identifiers/);
  assert.doesNotMatch(html, /<details[^>]*class="feedback-(?:plan-options|technical feedback-raw-audit)"[^>]* open/);
});

test('logs hide internal step state churn even from a legacy unfiltered event response', () => {
  const event = { audit_id: 'one', timestamp: '2026-10-23T00:00:00Z', actor: 'simulator',
    action: 'STATE:EXECUTING->PAUSED', reason: 'Safe pause at segment boundary', details: {} };
  const html = render(LogsView, { route: completedRoute, executionState: 'EXECUTING',
    logs: { execution_state: 'PAUSED', history_versions: [], audit_log: [event] },
  });
  const operator = html.split('<details class="feedback-technical feedback-raw-audit">')[0];
  assert.match(operator, /feedback-badge">Cleaning/);
  assert.doesNotMatch(operator, /Safe pause|STATE:|feedback-badge">Paused/);
  assert.match(html, /STATE:EXECUTING-&gt;PAUSED/);
});

test('planning decision uses recorded repeat comparisons without invented optimality or switching scores', () => {
  const props = { route: { ...completedRoute, status: 'PROPOSED', repeat_comparison: {
    candidates_checked: 4, feasible_candidates: 3,
    previous_estimated_completion_minutes: 125, new_estimated_completion_minutes: 124,
    sweep_pattern_changed: false, collection_order_changed: false, travel_path_changed: false,
    summary: 'The previous pattern was kept after checking the current conditions.',
  } } };
  const html = render(exports.PlanningDecisionSummary, props);
  assert.match(html, /Latest planning decision/);
  assert.match(html, /Path kept/);
  assert.match(html, /Options checked<\/dt><dd>4/);
  assert.match(html, /Options passing checks<\/dt><dd>3/);
  assert.match(html, /2h 5min/);
  assert.match(html, /2h 4min/);
  assert.match(html, /does not retrain the model/);
  assert.doesNotMatch(html, /optimal|switching gain|stability threshold|100% route feasible/i);
  const changed = render(exports.PlanningDecisionSummary, { route: { ...props.route, repeat_comparison: { ...props.route.repeat_comparison, travel_path_changed: true } } });
  assert.match(changed, /Plan updated/);
  assert.doesNotMatch(changed, /Path kept/);
});

test('planning decision shows failed checks and distinguishes remaining work from full-run times', () => {
  const failed = render(exports.PlanningDecisionSummary, { route: { ...completedRoute,
    status: 'DRAFT_FAILED_KPI', kpi_evaluation: { passed: false, failed_constraints: ['energy_reserve_below_minimum'] },
  } });
  assert.match(failed, /Needs attention/);
  assert.match(failed, /energy reserve below minimum/);
  assert.doesNotMatch(failed, /Ready for review|Path kept/);
  const revised = render(exports.PlanningDecisionSummary, { route: { ...completedRoute,
    replan_reason: 'obstacle_report_with_validated_exclusion', replan_comparison: {
      previous_remaining_duration_min: 61, revised_remaining_duration_min: 65, revised_remaining_distance_m: 1234,
    },
  } });
  assert.match(revised, /A new obstacle needed a safe detour/);
  assert.match(revised, /Previous remaining estimate/);
  assert.match(revised, /1h 5min/);
  assert.match(revised, /rather than two whole cleaning runs/);
  const complete = render(exports.PlanningDecisionSummary, { route: completedRoute });
  assert.match(complete, /Plan used for this completed run/);
  assert.doesNotMatch(complete, /approve it when|Ready for review/);
  const empty = render(exports.PlanningDecisionSummary, { route: null });
  assert.match(empty, /No plan yet/);
  assert.doesNotMatch(empty, /Options checked|Plan updated|Path kept/);
});

test('activity timeline marks actual safety and depot events and labels counts as recent', () => {
  const event = (action, index) => ({ audit_id: `timeline-${index}`, timestamp: `2026-10-23T00:0${index}:00Z`,
    actor: 'simulator', route_version: 2, action, reason: `Recorded ${action}`, details: {} });
  const audit = ['PLAN_SELECTED', 'DEPOT_RECHARGE', 'EMERGENCY_STOP', 'EVENT:OBSTACLE_DETECTED', 'COMPLETE_JOB'].map(event);
  const html = render(LogsView, { route: completedRoute, logs: { execution_state: 'COMPLETED', history_versions: [], audit_log: audit } });
  const visible = html.split('<details class="feedback-technical feedback-raw-audit">')[0];
  assert.match(visible, /5 recent events/);
  assert.match(visible, /Plan decisions<\/dt><dd>1/);
  assert.match(visible, /Depot services<\/dt><dd>1/);
  assert.match(visible, /Safety alerts<\/dt><dd>1/);
  assert.match(visible, /rather than lifetime totals/);
  assert.match(visible, /does not mean it has been resolved/);
  assert.match(visible, /feedback-event critical/);
  assert.match(visible, /feedback-event-badge critical">Safety stop/);
  assert.match(visible, /feedback-event-badge attention">Obstacle update/);
  assert.match(visible, /feedback-event-badge success">Depot service/);
  assert.match(visible, /dateTime="2026-10-23T00:02:00Z"|datetime="2026-10-23T00:02:00Z"/);
  assert.doesNotMatch(visible, /AI Decision|switching gain|fixed delay|anomalies cleared/i);
});
