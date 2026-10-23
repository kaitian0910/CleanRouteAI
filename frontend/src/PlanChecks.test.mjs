import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const source = readFileSync(new URL('./PlanChecks.tsx', import.meta.url), 'utf8').replace('import "./PlanChecks.css";', '');
const { outputText } = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
  jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
} });
const moduleExports = {};
new Function('require', 'exports', outputText)(createRequire(import.meta.url), moduleExports);
const { PlanChecks, TaskReview } = moduleExports;
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const metric = (actual, targets = {}) => ({ actual, passed: true, unit: '%', provenance: 'ESTIMATED', ...targets });
const route = {
  route_id: 'active-route', route_version: 3, machine_id: 'sweeper-alpha-01', status: 'EXECUTING',
  depot_cycles_enabled: true, minimum_battery_percent: 25, max_outing_duration_minutes: 80,
  payload_utilization_percent: 30,
  kpi_policy_snapshot: { min_energy_reserve_percent: 20, max_estimated_duration_minutes: 90, max_payload_utilization_percent: 85 },
  kpi_evaluation: { route_version: 3, passed: true, failed_constraints: [], metrics: {
    minimum_battery_percent: metric(25, { target_min: 20 }),
    max_outing_duration_minutes: metric(80, { target_max: 90, unit: 'min' }),
    payload_utilization_percent: metric(30, { target_max: 85 }),
    route_geometry_issues: metric(0, { target_max: 0, unit: 'sections', provenance: 'PLANNED' }),
    planned_collection_assignment_percent: metric(100, { target: 100, provenance: 'PLANNED' }),
    time_window_violations: metric(0, { target_max: 0, unit: 'tasks', provenance: 'PLANNED' }),
  } },
  tasks: [], skipped_tasks: [], task_schedule: [],
};

test('plan checks use saved configured limits rather than reference mockup values', () => {
  const html = render(PlanChecks, { route, telemetry: null, onOpenRoute: () => {} });
  assert.match(html, /Required reserve: 20%/);
  assert.match(html, /Limit: 90 min per outing/);
  assert.match(html, /limit 85%/);
  assert.match(html, /Planned checks passed/);
  assert.match(html, /Edit limits on Route map/);
  assert.doesNotMatch(html, /18%|15%|100% ROUTE FEASIBLE|switching gain|foot traffic/i);
  assert.match(html, /Photo classifications do not measure weight/);
  assert.match(html, /Collection tasks included in the plan/);
  assert.match(html, /not yet a completed pickup/);
});

test('load readings require both active route and machine, and planned percent stays separate', () => {
  const telemetry = { route_id: route.route_id, machine_id: route.machine_id, payload_kg: 12.5, payload_volume_m3: 0.125,
    battery_percent: 71, provenance: 'SIMULATED' };
  const html = render(PlanChecks, { route, telemetry });
  assert.match(html, /Latest simulated load: 12.5 kg \/ 0.125 m³/);
  assert.match(html, /Latest simulated battery: 71%/);
  assert.match(html, /30%/);
  for (const stale of [{ ...telemetry, route_id: 'old-route' }, { ...telemetry, machine_id: 'other-machine' }]) {
    const staleHtml = render(PlanChecks, { route, telemetry: stale });
    assert.doesNotMatch(staleHtml, /12.5 kg|battery: 71%/);
    assert.match(staleHtml, /No current load reading for this run/);
  }
  assert.match(render(PlanChecks, { route, telemetry: { ...telemetry, provenance: 'MEASURED_TELEMETRY' } }), /Latest reported load/);
});

test('missing and stale evaluations do not fabricate successful checks or defaults', () => {
  assert.match(render(PlanChecks, { route: null, telemetry: null }), /Choose an area/);
  const empty = { route_id: 'empty', machine_id: 'alpha', route_version: 1 };
  const html = render(PlanChecks, { route: empty, telemetry: null });
  assert.match(html, /Not evaluated yet/);
  assert.match(html, /Required reserve: Not available/);
  assert.match(html, /No evaluation recorded/);
  assert.doesNotMatch(html, /role="meter"|NaN|Infinity|Planned checks passed|20%|90 min/);
  const stale = render(PlanChecks, { route: { ...route, kpi_evaluation: { ...route.kpi_evaluation, route_version: 2 } }, telemetry: null });
  assert.match(stale, /Not evaluated yet/);
  assert.doesNotMatch(stale, /<strong>Access and obstacle clearance<\/strong><small>0 sections/);
});

test('completed checks retain the original outing forecast when remaining-work metrics reach zero', () => {
  const html = render(PlanChecks, { route: { ...route, status: 'COMPLETED',
    kpi_evaluation: { ...route.kpi_evaluation, metrics: { ...route.kpi_evaluation.metrics,
      max_outing_duration_minutes: metric(0, { target_max: 90, unit: 'min' }),
      minimum_battery_percent: metric(99, { target_min: 20 }),
    } },
  }, telemetry: null });
  assert.match(html, /Lowest planned battery<\/h3><div class="plan-check-value">25%/);
  assert.match(html, /Longest planned outing<\/h3><div class="plan-check-value">80 min/);
  assert.doesNotMatch(html, /Longest planned outing<\/h3><div class="plan-check-value">0 min/);
});

test('failed checks stay visible with valid zero readings and safe meters', () => {
  const unsafe = { ...route, minimum_battery_percent: 0, payload_utilization_percent: 0,
    kpi_evaluation: { ...route.kpi_evaluation, passed: false, failed_constraints: ['energy_reserve_below_minimum'],
      metrics: { ...route.kpi_evaluation.metrics, minimum_battery_percent: metric(0, { target_min: 20, passed: false }),
        route_geometry_issues: metric(2, { target_max: 0, unit: 'sections', passed: false }) } } };
  const html = render(PlanChecks, { route: unsafe, telemetry: null });
  assert.match(html, /Plan needs attention/);
  assert.match(html, /2 sections · maximum 0 sections/);
  assert.match(html, /energy_reserve_below_minimum/);
  assert.match(html, /aria-valuenow="0"/);
  assert.doesNotMatch(html, /NaN|Infinity/);
  const invalid = render(PlanChecks, { route: { ...route, kpi_policy_snapshot: { max_estimated_duration_minutes: 0 },
    kpi_evaluation: null, max_outing_duration_minutes: Number.NaN, payload_utilization_percent: Infinity }, telemetry: null });
  assert.doesNotMatch(invalid, /NaN|Infinity/);
});

const task = (id, extra = {}) => ({ task_id: id, task_type: 'RUBBISH_COLLECTION', priority: 7,
  service_duration_sec: 90, time_window_start_min: 10, time_window_end_min: 90, completed: false, ...extra });

test('task review separates completed, scheduled and skipped tasks using recorded data', () => {
  const html = render(TaskReview, { route: { ...route,
    tasks: [task('done', { completed: true }), task('pending'), task('blocked', { task_type: 'OVERSIZED_RUBBISH' })],
    task_schedule: [{ task_id: 'pending', service_start_min: 15, service_end_min: 16.5, disposal_before: true }],
    skipped_tasks: [{ task_id: 'done', reasons: ['old_reason'] }, { task_id: 'blocked', reasons: ['task_time_window_infeasible'] }],
  } });
  assert.match(html, /<dt>Finished<\/dt><dd>1<\/dd>/);
  assert.match(html, /<dt>Pending<\/dt><dd>1<\/dd>/);
  assert.match(html, /<dt>Needs attention<\/dt><dd>1<\/dd>/);
  assert.match(html, /Bin pickup 1/);
  assert.match(html, /Bulky pickup 3/);
  assert.match(html, /7 \/ 10/);
  assert.match(html, /1.5 min/);
  assert.match(html, /10 min to 90 min/);
  assert.match(html, /15 min to 16.5 min/);
  assert.match(html, /time window cannot be met/);
  assert.match(html, /disposal stop is planned/);
  assert.doesNotMatch(html, /pruned|hygiene risk score:|foot traffic score:/i);
});

test('skipped tasks absent from task list remain visible without invented priority or schedule', () => {
  const html = render(TaskReview, { route: { ...route, tasks: [], skipped_tasks: [
    { task_id: 'missing-original-task', reasons: ['unsafe_access'], recommended_actions: ['Inspect access at the site.'] },
  ] } });
  assert.match(html, /Bin pickup 1/);
  assert.match(html, /Not included/);
  assert.match(html, /<dt>Priority<\/dt><dd>Not recorded<\/dd>/);
  assert.match(html, /A safe access path could not be confirmed/);
  assert.match(html, /<details><summary>Task details/);
  assert.match(html, /missing-original-task/);
  assert.match(html, /Inspect access at the site/);
  assert.doesNotMatch(html, /NaN|Infinity|Scheduled<\/span>/);
  assert.match(render(TaskReview, { route }), /No individual pickups/);
  assert.match(render(TaskReview, { route: null }), /Create a cleaning plan/);
});
