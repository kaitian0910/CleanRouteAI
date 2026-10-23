import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const { outputText } = ts.transpileModule(readFileSync(new URL('./planningFeedback.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 },
});
const { formatMinutes, newTaskWindow, planningBlockMessage } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

test('resource block explains the evaluated values and configured limits', () => {
  const message = planningBlockMessage({ route_safety_issues: [], kpi_evaluation: {
    failed_constraints: ['duration_exceeds_target_max', 'energy_reserve_below_minimum'],
    metrics: { estimated_duration_minutes: { actual: 212.92, target_max: 90 },
      remaining_energy_reserve_percent: { actual: 12.5, target_min: 25 } },
  } });
  assert.match(message, /212.9 min exceeds the 90 min/);
  assert.match(message, /12.5%.*25%/);
  assert.match(message, /smaller area/);
});

test('geometry failures are not mislabeled as battery or duration failures', () => {
  const message = planningBlockMessage({ route_safety_issues: [], kpi_evaluation: {
    failed_constraints: ['route_geometry_unresolved'], metrics: {},
  } });
  assert.match(message, /could not produce a safe route connection/);
  assert.match(message, /route remains blocked/);
  assert.doesNotMatch(message, /route_geometry_unresolved|mapped water|outside the selected/);
  assert.doesNotMatch(message, /smaller area|battery/);
});

test('boundary geometry failure explains the clearance issue and identifies the actual segment', () => {
  const message = planningBlockMessage({
    route_safety_issues: ['segment seg-b653a9a8: clearance envelope leaves operating boundary'],
    kpi_evaluation: { failed_constraints: ['route_geometry_unresolved'], metrics: {} },
  });
  assert.match(message, /required clearance extends outside the selected operating area/);
  assert.match(message, /Details: segment seg-b653a9a8: clearance envelope leaves operating boundary/);
  assert.doesNotMatch(message, /Other failed checks|mapped water/);
});

test('water and disconnected diagnostics retain actual safety reasons and other failed gates', () => {
  const message = planningBlockMessage({ route_safety_issues: [
    "segment water-1: clearance envelope intersects impassable WATER_BODY 'Test lake'",
    'depot_cycle_return_unreachable: no permitted path to depot',
  ], kpi_evaluation: { failed_constraints: ['route_geometry_unresolved', 'travel_distance_exceeds_limit'], metrics: {} } });
  assert.match(message, /required clearance reaches mapped water/);
  assert.match(message, /Some points have no safe connecting path/);
  assert.match(message, /Test lake/);
  assert.match(message, /depot_cycle_return_unreachable/);
  assert.match(message, /Other failed checks: travel_distance_exceeds_limit/);
  assert.doesNotMatch(message, /outside the selected operating area/);
});

test('geometry details are deduplicated and bounded for a readable failure banner', () => {
  const message = planningBlockMessage({ route_safety_issues: [
    'same safety issue', 'same safety issue', 'second issue', 'third issue', 'fourth issue',
  ], kpi_evaluation: { failed_constraints: ['route_geometry_unresolved'], metrics: {} } });
  assert.equal(message.match(/same safety issue/g)?.length, 1);
  assert.match(message, /second issue; third issue/);
  assert.match(message, /1 more route safety issue\./);
  assert.doesNotMatch(message, /fourth issue/);
});

test('enabled depot cycles explain an infeasible outing rather than reject the whole job length', () => {
  const message = planningBlockMessage({ depot_cycles_enabled: true, route_safety_issues: [], kpi_evaluation: {
    failed_constraints: ['duration_exceeds_target_max'],
    metrics: { estimated_duration_minutes: { actual: 95, target_max: 90 } },
  } });
  assert.match(message, /outing cannot safely fit/);
  assert.match(message, /safe return path/);
  assert.doesNotMatch(message, /Select a smaller area/);
});

test('missed bin deadlines identify task, required service and mission-minute window', () => {
  const message = planningBlockMessage({ depot_cycles_enabled: true, route_safety_issues: [],
    tasks: [{ task_id: 'bin-1', time_window_start_min: 0, time_window_end_min: 90, service_duration_sec: 45 },
      { task_id: 'bin-2', time_window_start_min: 10, time_window_end_min: 60, service_duration_sec: 45 }],
    skipped_tasks: [{ task_id: 'bin-1', reasons: ['task_time_window_infeasible'] }],
    task_schedule: [{ task_id: 'bin-2', arrival_min: 61.23456, service_end_min: 61.98456, window_start_min: 10, window_end_min: 60, status: 'MISSED' }],
    kpi_evaluation: { failed_constraints: ['time_window_violation'], metrics: {} },
  });
  assert.match(message, /bin-1: no feasible service slot/);
  assert.match(message, /0.0 min to 90.0 min/);
  assert.match(message, /45 s service/);
  assert.match(message, /bin-2: arrival 61.2 min, service finish 62.0 min/);
  assert.match(message, /route remains blocked/);
  assert.match(message, /collect deadline-sensitive bins before long sweeping/);
  assert.doesNotMatch(message, /Select a smaller area/);
});

test('completion minutes use a stable readable precision', () => {
  assert.equal(formatMinutes(654.244165991), '654.2 min');
  assert.equal(formatMinutes(0), '0.0 min');
  assert.equal(formatMinutes(undefined), 'N/A');
});

test('backend task-window failure key explains unassigned collection count', () => {
  const message = planningBlockMessage({ route_safety_issues: [],
    tasks: [{ task_id: 'bin-1', task_type: 'RUBBISH_COLLECTION', completed: false }],
    task_schedule: [], skipped_tasks: [],
    kpi_evaluation: { failed_constraints: ['task_time_window_infeasible', 'collection_completion_below_target'], metrics: {} },
  });
  assert.match(message, /Collection deadlines could not be met/);
  assert.match(message, /0\/1 collection tasks/);
  assert.doesNotMatch(message, /Other failed checks/);
});

test('new bins and YOLO tasks use the current mission clock for their initial window', () => {
  assert.deepEqual(newTaskWindow(200), { time_window_start_min: 200, time_window_end_min: 290 });
  assert.deepEqual(newTaskWindow(200, 480), { time_window_start_min: 200, time_window_end_min: 680 });
  assert.deepEqual(newTaskWindow(0), { time_window_start_min: 0, time_window_end_min: 90 });
  assert.deepEqual(newTaskWindow(Number.NaN), { time_window_start_min: 0, time_window_end_min: 90 });
});
