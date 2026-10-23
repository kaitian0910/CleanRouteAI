import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source = readFileSync(new URL('./routeExecution.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } });
const { RouteExecutionSession, collectedWasteStats } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

const response = data => ({ ok: true, json: async () => data });

test('fresh planning can replace a route lost after a backend reload, but control failures remain blocking', async () => {
  const missing = new RouteExecutionSession('http://local', 'token', async () => ({
    ok: false, status: 404, json: async () => ({ detail: 'Route not found' }),
  }));
  assert.equal(await missing.cancelRoute('old-route'), null);
  for (const status of [401, 403, 409, 500]) {
    const blocked = new RouteExecutionSession('http://local', 'token', async () => ({
      ok: false, status, json: async () => ({ detail: 'Control rejected' }),
    }));
    await assert.rejects(() => blocked.cancelRoute('r'), error => error.status === status);
  }
  const unavailable = new RouteExecutionSession('http://local', 'token', async () => { throw new Error('Network failure'); });
  await assert.rejects(() => unavailable.cancelRoute('r'), /Network failure/);
});

test('simulation executes one server segment, resumes serially and stops at completion', async () => {
  const calls = [];
  let steps = 0;
  let inFlight = 0;
  const request = async (url, options) => {
    calls.push(url);
    if (options.method === 'POST') {
      assert.equal(inFlight++, 0);
      assert.deepEqual(JSON.parse(options.body), { stop_after_segments: 1, expected_completed_segments: steps, idempotency_key: `r:step:${steps}` });
      await new Promise(resolve => setTimeout(resolve, 5));
      steps++; inFlight--;
      return response({});
    }
    if (url.endsWith('/metrics')) return response({ execution_state: steps === 2 ? 'COMPLETED' : steps === 0 ? 'READY' : 'PAUSED', telemetry_frames: [] });
    return response({ route_id: 'r', status: steps === 2 ? 'COMPLETED' : 'EXECUTING', segments: [0, 1].map(index => ({ completed: index < steps })) });
  };
  const session = new RouteExecutionSession('http://local', 'token', request);
  await new Promise((resolve, reject) => session.start({ route_id: 'r', status: 'APPROVED' }, snapshot => {
    if (snapshot.metrics.execution_state === 'COMPLETED') resolve();
  }, reject, 0));
  await session.settle();
  assert.equal(steps, 2);
  assert.equal(calls.filter(url => url.endsWith('/execute')).length, 1);
  assert.equal(calls.filter(url => url.endsWith('/resume')).length, 1);
});

test('stopping during a server request suppresses stale UI updates but settles the accepted step', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const session = new RouteExecutionSession('', '', async (url, options) => {
    if (options.method === 'POST') await pending;
    return response(url.endsWith('/metrics') ? { execution_state: 'READY', telemetry_frames: [] }
      : { route_id: 'old', segments: [{ completed: false }] });
  });
  let updates = 0;
  session.start({ route_id: 'old', status: 'APPROVED' }, () => updates++, () => updates++);
  await new Promise(resolve => setTimeout(resolve, 0));
  session.stop();
  release();
  await session.settle();
  assert.equal(updates, 0);
});

test('depot recharge advances simulated time and automatically resumes unfinished work', async () => {
  const segments = [
    { segment_id: 'return', segment_type: 'RETURN_TO_BASE', completed: false },
    { segment_id: 'service', segment_type: 'DEPOT_RECHARGE', completed: false },
    { segment_id: 'remaining', segment_type: 'SWEEP_SWATH', completed: false },
    { segment_id: 'final-return', segment_type: 'RETURN_TO_BASE', completed: false },
  ];
  let steps = 0;
  const actions = [];
  const snapshots = [];
  const request = async (url, options) => {
    if (options.method === 'POST') {
      actions.push(url.split('/').at(-1));
      assert.deepEqual(JSON.parse(options.body), { stop_after_segments: 1, expected_completed_segments: steps,
        idempotency_key: `multi-outing:step:${steps}` });
      segments[steps++].completed = true;
      return response({});
    }
    if (url.endsWith('/metrics')) return response({
      execution_state: steps === 4 ? 'COMPLETED' : steps === 0 ? 'READY' : 'PAUSED',
      telemetry_frames: steps ? [{ active_segment_id: segments[steps - 1].segment_id, mission_elapsed_min: steps >= 2 ? 25 + steps : steps,
        battery_percent: steps === 1 ? 25 : 95 }] : [],
    });
    return response({ route_id: 'multi-outing', status: steps === 4 ? 'COMPLETED' : 'EXECUTING', segments: structuredClone(segments) });
  };
  const session = new RouteExecutionSession('http://local', 'token', request);
  await new Promise((resolve, reject) => session.start({ route_id: 'multi-outing', status: 'APPROVED' }, snapshot => {
    snapshots.push(snapshot);
    if (snapshot.metrics.execution_state === 'COMPLETED') resolve();
  }, reject, 0));
  await session.settle();
  assert.deepEqual(actions, ['execute', 'resume', 'resume', 'resume']);
  assert.equal(snapshots[1].metrics.telemetry_frames[0].active_segment_id, 'service');
  assert.equal(snapshots[1].metrics.telemetry_frames[0].battery_percent, 95);
  assert.equal(snapshots[1].route.segments[2].completed, false);
  assert.equal(snapshots[1].route.status, 'EXECUTING');
  assert.equal(snapshots[3].route.status, 'COMPLETED');
});

test('default transport calls fetch with the browser global receiver', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = function (url) {
    assert.equal(this, globalThis, 'native Window.fetch requires its Window receiver');
    calls++;
    assert.equal(url, 'http://local/api/health');
    return Promise.resolve(response({ ok: true }));
  };
  try {
    const session = new RouteExecutionSession('http://local', 'token');
    assert.deepEqual(await session.json('/health'), { ok: true });
    assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});

test('a lost response reconciles completed work before resuming the next step', async () => {
  let steps = 0;
  let loseResponse = true;
  const posts = [];
  const request = async (url, options) => {
    if (options.method === 'POST') {
      const body = JSON.parse(options.body);
      posts.push({ action: url.split('/').at(-1), ...body });
      assert.equal(body.expected_completed_segments, steps);
      steps++;
      if (loseResponse) { loseResponse = false; throw new TypeError('Network response lost'); }
      return response({});
    }
    if (url.endsWith('/metrics')) return response({ execution_state: steps === 0 ? 'READY' : steps === 2 ? 'COMPLETED' : 'PAUSED', telemetry_frames: [] });
    return response({ route_id: 'lost', segments: [0, 1].map(i => ({ completed: i < steps })) });
  };
  const session = new RouteExecutionSession('', '', request);
  const staleRoute = { route_id: 'lost', status: 'APPROVED' };
  await new Promise(resolve => session.start(staleRoute, () => assert.fail('No response was received'), error => {
    assert.match(error.message, /lost/); resolve();
  }, 0));
  await session.settle();
  await new Promise((resolve, reject) => session.start(staleRoute, snapshot => {
    if (snapshot.metrics.execution_state === 'COMPLETED') resolve();
  }, reject, 0));
  await session.settle();
  assert.deepEqual(posts.map(p => [p.action, p.expected_completed_segments, p.idempotency_key]),
    [['execute', 0, 'lost:step:0'], ['resume', 1, 'lost:step:1']]);
  assert.equal(steps, 2);
});

test('already completed server state never issues another movement request', async () => {
  const session = new RouteExecutionSession('', '', async (url, options) => {
    assert.equal(options.method, 'GET');
    return response(url.endsWith('/metrics') ? { execution_state: 'COMPLETED', telemetry_frames: [] }
      : { route_id: 'done', segments: [{ completed: true }] });
  });
  await new Promise((resolve, reject) => session.start({ route_id: 'done', status: 'APPROVED' }, resolve, reject, 0));
  await session.settle();
});

test('failed execution does not fabricate completed segments or keep running', async () => {
  let calls = 0;
  const session = new RouteExecutionSession('', '', async (_, options) => {
    assert.equal(options.method, 'GET', 'failed reconciliation must not issue a movement');
    calls++;
    return { ok: false, status: 409, json: async () => ({ detail: 'EMERGENCY_STOP active' }) };
  });
  let error;
  session.start({ route_id: 'r', status: 'APPROVED' }, () => assert.fail('No update expected'), value => { error = value; });
  await session.settle();
  assert.match(error.message, /EMERGENCY_STOP/);
  assert.equal(calls, 2); // Route and metrics are reconciled together.
});

test('collected totals are derived from completed tasks and their estimates', () => {
  const bottle = { completed: true, task_type: 'RUBBISH_COLLECTION', waste_observation: { waste_class: 'Bottle' }, rubbish_spec: { estimated_weight_kg: 0.05 } };
  const tasks = [bottle, { ...bottle, completed: false }, { completed: true, task_type: 'RUBBISH_COLLECTION' }];
  const stats = collectedWasteStats(tasks);
  assert.equal(stats.plasticAndCans, 1);
  assert.equal(stats.binsEmptied, 1);
  assert.equal(stats.totalWeightKg, 0.05);
  assert.deepEqual(collectedWasteStats(tasks), stats);
});

test('material classes keep ambiguous and unrecognized waste in Others', () => {
  const plasticLabels = ['Bottle', 'Cap', 'PlasticBag', 'Wrapper', 'Can', 'PlasticCup', 'plastic bag', 'PLASTIC_CUP'];
  const paperLabels = ['Paper', 'Cardboard'];
  const otherLabels = ['Cigarette', 'Cup', 'OtherLitter', 'UnknownMaterial', '', null];
  const task = waste_class => ({ task_type: 'RUBBISH_COLLECTION', completed: true, waste_observation: { waste_class }, rubbish_spec: { estimated_weight_kg: 0.1 } });
  const tasks = [...plasticLabels, ...paperLabels, ...otherLabels].map(task);
  const result = collectedWasteStats(tasks);
  assert.equal(result.plasticAndCans, plasticLabels.length);
  assert.equal(result.paperAndPackaging, paperLabels.length);
  assert.equal(result.others, otherLabels.length);
  assert.equal(result.binsEmptied, 0);
  assert.equal(result.bulkyItems, 0);
  assert.equal(result.totalWeightKg, tasks.length * 0.1);
});

test('only completed collections count and weight is counted once regardless of category', () => {
  const tasks = [
    { task_id: 'other-detected', task_type: 'RUBBISH_COLLECTION', completed: false, waste_observation: { waste_class: 'OtherLitter' }, rubbish_spec: { estimated_weight_kg: 20 } },
    { task_id: 'other-collected', task_type: 'RUBBISH_COLLECTION', completed: true, waste_observation: { waste_class: 'Cup' }, rubbish_spec: { estimated_weight_kg: 0.25 } },
    { task_id: 'bulky-collected', task_type: 'OVERSIZED_RUBBISH', completed: true, waste_observation: { waste_class: 'Cardboard' }, rubbish_spec: { estimated_weight_kg: 8 } },
    { task_id: 'bin-collected', task_type: 'RUBBISH_COLLECTION', completed: true, rubbish_spec: { estimated_weight_kg: 3 } },
  ];
  const result = collectedWasteStats(tasks);
  assert.deepEqual(result, { others: 1, plasticAndCans: 0, paperAndPackaging: 0, binsEmptied: 1, bulkyItems: 1, totalWeightKg: 11.25 });
  assert.deepEqual(collectedWasteStats(tasks), result, 'refreshing the same completed tasks does not accumulate counts');
});

test('all eight trained YOLO classes map to their intended collection material', () => {
  const actualModelClasses = ['Plastic Bag & Wrapper', 'Bottle', 'Bottle Cap & Lid', 'Can', 'Paper & Cardboard', 'Cigarette', 'Cup', 'Other Litter'];
  const tasks = actualModelClasses.map((waste_class, index) => ({
    task_id: `model-class-${index}`, task_type: 'RUBBISH_COLLECTION', completed: true,
    waste_observation: { waste_class }, rubbish_spec: { estimated_weight_kg: 0.125 },
  }));
  assert.deepEqual(collectedWasteStats(tasks), {
    plasticAndCans: 4, paperAndPackaging: 1, others: 3,
    binsEmptied: 0, bulkyItems: 0, totalWeightKg: 1,
  });
});
