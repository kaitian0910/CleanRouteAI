import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const source = readFileSync(new URL('./MissionStatusStrip.tsx', import.meta.url), 'utf8')
  .replace('import "./MissionStatusStrip.css";', '');
const { outputText } = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
  jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
} });
const exports = {};
new Function('require', 'exports', outputText)(createRequire(import.meta.url), exports);
const { missionStatusValues, MissionStatusStripView } = exports;
const route = { route_id: 'current', machine_id: 'alpha', status: 'APPROVED', approved_by: 'operator',
  mission_elapsed_min: 12, kpi_policy_snapshot: { min_energy_reserve_percent: 27 },
  segments: [{ segment_id: 'clean', segment_type: 'SWEEP_SWATH', completed: false }] };
const props = { apiBase: '/test', token: 'test', machineId: 'alpha', route,
  executionState: 'EXECUTING', telemetry: null, batteryPercent: 83 };
const frame = { route_id: 'current', machine_id: 'alpha', mission_elapsed_min: 97.9,
  battery_percent: 25, payload_kg: 45, active_segment_id: 'clean' };
const machine = { machine_id: 'alpha', current_payload_kg: 7, max_payload_kg: 150, current_battery_percent: 94 };

test('overview uses current route telemetry, configured reserve and actual weight capacity', () => {
  const values = missionStatusValues({ ...props, telemetry: frame }, machine);
  assert.equal(values.elapsed, '1h 37min');
  assert.equal(values.battery, '25%');
  assert.equal(values.batteryNote, 'Minimum reserve 27%');
  assert.equal(values.batteryLow, true);
  assert.equal(values.payload, '45 kg / 150 kg');
  assert.equal(values.payloadNote, '30% by weight');
  assert.equal(values.activity, 'Cleaning area');
});

test('frames from another route or machine never populate mission time, load or battery', () => {
  for (const stale of [{ ...frame, route_id: 'old' }, { ...frame, machine_id: 'beta' }]) {
    const values = missionStatusValues({ ...props, telemetry: stale }, machine);
    assert.equal(values.elapsed, '12min');
    assert.equal(values.battery, '83%');
    assert.equal(values.payload, '7 kg / 150 kg');
    assert.match(values.payloadNote, /machine snapshot/);
  }
  const otherSelectedMachine = missionStatusValues({ ...props, machineId: 'beta', telemetry: frame }, machine);
  assert.equal(otherSelectedMachine.activity, 'No active job');
  assert.equal(otherSelectedMachine.elapsed, '—');
  assert.equal(otherSelectedMachine.battery, '—');
  assert.equal(otherSelectedMachine.payload, '—');
  assert.equal(otherSelectedMachine.batteryNote, 'No active plan reserve');
});

test('completed depot telemetry does not claim continued recharging', () => {
  const afterService = { ...route, segments: [
    { segment_id: 'charge', segment_type: 'DEPOT_RECHARGE', completed: true },
    { segment_id: 'resume', segment_type: 'TRANSIT', completed: false },
  ] };
  assert.equal(missionStatusValues({ ...props, route: afterService,
    telemetry: { ...frame, active_segment_id: 'charge' } }, machine).activity, 'Travelling to next task');
  const finished = missionStatusValues({ ...props, route: { ...afterService, status: 'COMPLETED' },
    executionState: 'COMPLETED', telemetry: { ...frame, active_segment_id: 'charge' } }, machine);
  assert.equal(finished.activity, 'Cleaning finished');
  assert.doesNotMatch(finished.activity, /dock|recharg/i);
});

test('safety state, pause and planning override route progress truthfully', () => {
  const completed = { ...route, status: 'COMPLETED' };
  assert.equal(missionStatusValues({ ...props, route: completed, executionState: 'EMERGENCY_STOP' }).activity, 'Emergency stop');
  assert.equal(missionStatusValues({ ...props, telemetry: { ...frame, emergency_stop_engaged: true } }).activity, 'Emergency stop');
  assert.equal(missionStatusValues({ ...props, executionState: 'NEEDS_ASSISTANCE' }).activity, 'Needs attention');
  assert.equal(missionStatusValues({ ...props, executionState: 'PAUSED' }).activity, 'Paused');
  assert.equal(missionStatusValues({ ...props, route: completed, executionState: 'PLANNING' }).activity, 'Planning route');
  assert.equal(missionStatusValues({ ...props, executionState: 'REPLANNING' }).activity, 'Updating route');
});

test('depot return, pending service and unavailable resources use explicit labels', () => {
  assert.equal(missionStatusValues({ ...props, route: { ...route,
    segments: [{ segment_id: 'home', segment_type: 'RETURN_TO_BASE', completed: false }] } }).activity, 'Returning to depot');
  assert.equal(missionStatusValues({ ...props, route: { ...route,
    segments: [{ segment_id: 'charge', segment_type: 'DEPOT_RECHARGE', completed: false }] } }).activity, 'Depot service next');
  const unavailable = missionStatusValues({ ...props, route: null, executionState: 'IDLE' });
  assert.equal(unavailable.payload, '—');
  assert.equal(unavailable.payloadNote, 'Capacity unavailable');
  assert.equal(unavailable.battery, '—');
  assert.equal(unavailable.elapsed, '—');
});

test('zero reserve is valid and overloaded payload is shown rather than capped', () => {
  const values = missionStatusValues({ ...props, route: { ...route,
    kpi_policy_snapshot: { min_energy_reserve_percent: 0 } }, telemetry: { ...frame, payload_kg: 165 } }, machine);
  assert.equal(values.batteryNote, 'Minimum reserve 0%');
  assert.equal(values.batteryLow, false);
  assert.equal(values.payloadNote, '110% by weight');
  assert.equal(values.payloadFull, true);
});

test('reserve falls back to the recorded evaluation and remains unknown without configuration', () => {
  const noPolicy = { ...route, kpi_policy_snapshot: undefined };
  assert.equal(missionStatusValues({ ...props, route: noPolicy }).batteryNote, 'Reserve unavailable');
  const evaluated = { ...noPolicy, kpi_evaluation: { passed: true,
    metrics: { remaining_energy_reserve_percent: { target_min: 32 } } } };
  assert.equal(missionStatusValues({ ...props, route: evaluated }).batteryNote, 'Minimum reserve 32%');
});

test('overview renders a compact labelled simulation without invented hardware readings', () => {
  const html = renderToStaticMarkup(React.createElement(MissionStatusStripView,
    { values: missionStatusValues({ ...props, telemetry: frame }, machine) }));
  assert.match(html, /aria-label="Mission overview — simulation"/);
  assert.match(html, /Simulated elapsed time/);
  assert.match(html, /Waste load/);
  assert.match(html, /Minimum reserve 27%/);
  assert.doesNotMatch(html, /Campus clock|optimal|GPS|YOLO Active|15%|End:|projected/i);
});
