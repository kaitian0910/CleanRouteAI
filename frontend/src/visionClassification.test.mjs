import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('./visionClassification.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
} });
const exports = {};
new Function('exports', outputText)(exports);
const { VISION_CLASSIFICATION_CONFIDENCE, classifyVisionObservation,
  isUncertainVisionObservation, summarizeVisionDetections } = exports;

const observation = (label, confidence = 0.9, detection_id = label) => ({ detection_id, waste_class: label, confidence });

test('all eight supplied model classes map to material categories, with punctuation normalized', () => {
  for (const label of ['Plastic bag / wrapper', 'Bottle', 'Bottle cap / lid', 'Can']) {
    assert.equal(classifyVisionObservation(observation(label)), 'plasticAndCans', label);
  }
  assert.equal(classifyVisionObservation(observation('Paper / cardboard')), 'paperAndPackaging');
  for (const label of ['Cup', 'Cigarette', 'Other litter']) {
    assert.equal(classifyVisionObservation(observation(label)), 'others', label);
    assert.equal(isUncertainVisionObservation(observation(label)), false, label);
  }
});

test('uncertain predictions become Others without pretending the material is known', () => {
  assert.equal(VISION_CLASSIFICATION_CONFIDENCE, 0.6);
  for (const score of [undefined, null, NaN, Infinity, -Infinity, -0.1, 0.59, 1.1]) {
    const item = { detection_id: 'bottle', waste_class: 'Bottle', confidence: score };
    assert.equal(classifyVisionObservation(item), 'others', String(score));
    assert.equal(isUncertainVisionObservation(item), true, String(score));
  }
  for (const label of [undefined, null, '', 'an unrecognized object', 'constructor', 'toString']) {
    const item = observation(label);
    assert.equal(classifyVisionObservation(item), 'others', String(label));
    assert.equal(isUncertainVisionObservation(item), true, String(label));
  }
});

test('the classification boundary is inclusive and a stricter threshold is respected', () => {
  assert.equal(classifyVisionObservation(observation('Bottle', 0.6)), 'plasticAndCans');
  assert.equal(classifyVisionObservation(observation('Bottle', 0.8), 0.85), 'others');
  assert.equal(classifyVisionObservation(observation('Bottle', 0.85), 0.85), 'plasticAndCans');
});

test('one inference result counts each detection ID once and reports uncertainty separately from Others', () => {
  const detections = [observation('Bottle'), observation('Can'), observation('Paper / cardboard'),
    observation('Cup'), observation('Bottle', 0.4, 'uncertain-bottle'), observation('unrecognized item'),
    observation('Bottle'), observation('Can', 0.95, ' Can ')];
  assert.deepEqual(summarizeVisionDetections(detections), {
    plasticAndCans: 2, paperAndPackaging: 1, others: 3, totalItems: 6, uncertainItems: 2,
  });
  assert.equal(summarizeVisionDetections(detections).totalItems, 6, 'checking the same result does not accumulate a second frame');
});

test('distinct detections without IDs are counted individually and empty input resets every category', () => {
  assert.deepEqual(summarizeVisionDetections([
    { waste_class: 'Bottle', confidence: 0.9 }, { waste_class: 'Bottle', confidence: 0.9 },
    { detection_id: '', waste_class: 'Paper / cardboard', confidence: 0.9 },
  ]), { plasticAndCans: 2, paperAndPackaging: 1, others: 0, totalItems: 3, uncertainItems: 0 });
  assert.deepEqual(summarizeVisionDetections([]), {
    plasticAndCans: 0, paperAndPackaging: 0, others: 0, totalItems: 0, uncertainItems: 0,
  });
});
