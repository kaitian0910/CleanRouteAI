import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const { outputText } = ts.transpileModule(readFileSync(new URL('./SitePlanningPanel.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
});
const exports = {};
const require = createRequire(import.meta.url);
new Function('require', 'exports', outputText)(name => name === './planningSession' ? { apiErrorMessage: value => String(value) } : require(name), exports);
const { parseSiteGeoJson, validateOutingLimits } = exports;

test('site input accepts GeoJSON collections and rejects malformed or oversized files', () => {
  const featureCollection = { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { role: 'boundary' }, geometry: { type: 'Polygon', coordinates: [[[101, 3], [101.01, 3], [101.01, 3.01], [101, 3]]] } }] };
  assert.deepEqual(parseSiteGeoJson(JSON.stringify(featureCollection)), featureCollection);
  assert.throws(() => parseSiteGeoJson('{'), SyntaxError);
  assert.throws(() => parseSiteGeoJson('null'), /GeoJSON FeatureCollection/);
  assert.throws(() => parseSiteGeoJson('{"type":"Polygon","coordinates":[]}'), /GeoJSON FeatureCollection/);
  assert.throws(() => parseSiteGeoJson(' '.repeat(2 * 1024 * 1024 + 1)), /2 MiB/);
});

test('outing settings keep a positive return budget and a meaningful battery reserve', () => {
  const limits = { max_estimated_duration_minutes: 90, min_energy_reserve_percent: 20, max_travel_distance_m: 15000 };
  assert.equal(validateOutingLimits(limits), null);
  assert.match(validateOutingLimits({ ...limits, max_estimated_duration_minutes: 0 }), /duration/);
  assert.match(validateOutingLimits({ ...limits, min_energy_reserve_percent: 100 }), /reserve/);
  assert.match(validateOutingLimits({ ...limits, min_energy_reserve_percent: -1 }), /reserve/);
  assert.match(validateOutingLimits({ ...limits, min_energy_reserve_percent: 0 }), /reserve/);
  assert.match(validateOutingLimits({ ...limits, min_energy_reserve_percent: 99 }), /reserve/);
  assert.equal(validateOutingLimits({ ...limits, min_energy_reserve_percent: 5 }), null);
  assert.equal(validateOutingLimits({ ...limits, min_energy_reserve_percent: 95 }), null);
  assert.match(validateOutingLimits({ ...limits, max_travel_distance_m: Number.NaN }), /travel/);
});
