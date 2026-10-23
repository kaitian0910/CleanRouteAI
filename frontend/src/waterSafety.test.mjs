import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('./waterSafety.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } });
const { combineWaterFeatures, pointTouchesWater, pathTouchesWater, boundaryTouchesWater,
  offlineWaterConflict, automaticDryLocations, interiorWaterExample } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

const point = (x, y) => ({ lat: y / 110540, lon: x / 111320 });
const square = (x1, y1, x2, y2) => [point(x1, y1), point(x2, y1), point(x2, y2), point(x1, y2)];
const water = { feature_id: 'water-test', name: 'Test lake', feature_type: 'WATER_BODY',
  clearance_buffer_m: 1, polygon: { vertices: square(10, 10, 30, 30) } };

test('water excludes shoreline and clearance even when both path endpoints are dry', () => {
  assert.equal(pointTouchesWater(point(20, 20), water), true);
  assert.equal(pointTouchesWater(point(9.5, 20), water), true);
  assert.equal(pointTouchesWater(point(8, 20), water), false);
  assert.equal(pathTouchesWater([point(0, 20), point(40, 20)], water), true);
  assert.equal(pathTouchesWater([point(0, 9.5), point(40, 9.5)], water), true);
  assert.equal(pathTouchesWater([point(0, 8), point(40, 8)], water), false);
});

test('water intersection handles contained polygons, crossing edges and boundary contact', () => {
  assert.equal(boundaryTouchesWater(square(0, 0, 40, 40), water), true);
  assert.equal(boundaryTouchesWater(square(15, 15, 20, 20), water), true);
  assert.equal(boundaryTouchesWater(square(0, 19, 40, 21), water), true);
  assert.equal(boundaryTouchesWater(square(0, 0, 9, 40), water), true);
  assert.equal(boundaryTouchesWater(square(0, 0, 8, 8), water), false);
});

test('custom site omission or a duplicate ID cannot erase the mapped water safety geometry', () => {
  const custom = { ...water, polygon: { vertices: square(100, 100, 110, 110) } };
  assert.deepEqual(combineWaterFeatures([], [water]), [water]);
  assert.deepEqual(combineWaterFeatures([custom, { ...water, feature_id: 'building', feature_type: 'BUILDING' }], [water]), [water]);
});

test('offline preview fails closed for water in the area, a pin, a connector or a planned segment', () => {
  const params = { boundary: square(0, 0, 8, 8), start: point(2, 2), depot: point(6, 2),
    taskLocations: [], waterFeatures: [water] };
  assert.equal(offlineWaterConflict(params), null);
  for (const override of [
    { boundary: square(0, 0, 40, 40) },
    { depot: point(20, 20) },
    { start: point(0, 20), depot: point(40, 20) },
    { taskLocations: [point(20, 20)] },
    { segmentPaths: [[point(0, 20), point(40, 20)]] },
  ]) {
    assert.match(offlineWaterConflict({ ...params, ...override }), /Start the backend/);
  }
});

test('automatically generated pins move onto dry land without mutating supplied coordinates', () => {
  const proposed = { start: point(20, 20), depot: point(35, 5), taskA: point(3, 3) };
  const saved = structuredClone(proposed);
  const result = automaticDryLocations(square(0, 0, 40, 40), proposed, [water]);
  assert.equal(result.adjusted, true);
  assert.equal(result.noDryPoint, false);
  assert.equal(pointTouchesWater(result.start, water, 1), false);
  assert.deepEqual(result.depot, proposed.depot);
  assert.deepEqual(result.taskA, proposed.taskA);
  assert.deepEqual(proposed, saved);
});

test('an entirely submerged area has no dry defaults and is reported rather than moving pins outside it', () => {
  const proposed = { start: point(15, 15), depot: point(25, 15) };
  const result = automaticDryLocations(square(12, 12, 28, 28), proposed, [water]);
  assert.equal(result.noDryPoint, true);
  assert.deepEqual(result.start, proposed.start);
  assert.deepEqual(result.depot, proposed.depot);
});

test('the blocked-water example is derived inside the source shoreline', () => {
  const example = interiorWaterExample(water);
  assert.ok(example);
  const surface = { ...water, clearance_buffer_m: 0 };
  for (const p of [example.center, example.startLocation, example.disposalLocation, ...example.boundary]) {
    assert.equal(pointTouchesWater(p, surface), true);
  }
  assert.deepEqual(interiorWaterExample(water), example);
  assert.equal(interiorWaterExample(undefined), null);
});

test('the bundled lake example follows the shared OSM shoreline instead of the old guessed rectangle', () => {
  const data = JSON.parse(readFileSync(new URL('../../shared/perdana-water.json', import.meta.url), 'utf8'));
  const lake = data.features.find(feature => feature.feature_id === 'water-osm-relation-8429206-39923096');
  assert.ok(lake.polygon.vertices.length > 100);
  assert.equal(pointTouchesWater({ lat: 3.14155, lon: 101.68685 }, lake), false);
  const example = interiorWaterExample(lake);
  assert.ok(example);
  for (const p of [...example.boundary, example.startLocation, example.disposalLocation]) {
    assert.equal(pointTouchesWater(p, { ...lake, clearance_buffer_m: 0 }), true);
  }
});
