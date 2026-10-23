import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source = readFileSync(new URL('./planningSession.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } });
const { PlanningSession, apiErrorMessage, areaLocations } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

test('clear invalidates a pending plan and approval response', async () => {
  const session = new PlanningSession();
  const request = session.begin();
  const approvalIsCurrent = session.current();
  let resolve;
  const response = new Promise(done => { resolve = done; });
  let route = null;
  const pending = response.then(value => { if (request.isCurrent()) route = value; });
  session.cancel();
  resolve('old route');
  await pending;
  assert.equal(route, null);
  assert.equal(request.signal.aborted, true);
  assert.equal(approvalIsCurrent(), false);
});

test('only the newest drawing may update the map', () => {
  const session = new PlanningSession();
  const first = session.begin();
  const second = session.begin();
  assert.equal(first.isCurrent(), false);
  assert.equal(first.signal.aborted, true);
  assert.equal(second.isCurrent(), true);
});

test('FastAPI validation arrays are displayed as text', () => {
  assert.equal(apiErrorMessage([{loc: ['body', 'operating_boundary'], msg: 'Invalid polygon'}]),
    'body.operating_boundary: Invalid polygon');
});

test('small areas use interior task locations rather than fixed GPS offsets', () => {
  const vertices = [{lat: 3, lon: 101}, {lat: 3, lon: 101.00004},
    {lat: 3.00004, lon: 101.00004}, {lat: 3.00004, lon: 101}];
  for (const p of Object.values(areaLocations(vertices))) {
    assert.ok(p.lat > 3 && p.lat < 3.00004);
    assert.ok(p.lon > 101 && p.lon < 101.00004);
  }
});

test('concave polygon defaults remain inside the selected area', () => {
  const vertices = [{lat:0,lon:0},{lat:0,lon:4},{lat:1,lon:4},
    {lat:1,lon:1},{lat:4,lon:1},{lat:4,lon:0}];
  for (const p of Object.values(areaLocations(vertices))) {
    assert.ok(p.lat < 1 || p.lon < 1);
    assert.ok(p.lat > 0 && p.lon > 0);
  }
});
