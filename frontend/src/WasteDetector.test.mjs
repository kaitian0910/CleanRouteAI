import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const load = (file, dependencies = {}) => {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8').replace('import "./WasteDetector.css";', '');
  const { outputText } = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } });
  const exports = {};
  new Function('require', 'exports', outputText)(name => dependencies[name] || require(name), exports);
  return exports;
};
const planner = load('./planningSession.ts');
const classifications = load('./visionClassification.ts');
const { default: WasteDetector, DetectionSettings, DetectionResults, DEFAULT_DETECTION_CONFIDENCE } =
  load('./WasteDetector.tsx', { './planningSession': planner, './visionClassification': classifications });
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const noop = () => {};
const observation = { detection_id: 'bottle-one', waste_class: 'Bottle', confidence: 0.82 };
const resultsProps = {
  result: { model: 'best.pt', image_width: 100, image_height: 100, inference_ms: 50, detections: [observation] },
  confidence: 0.25, busy: false, onResetDefault: noop,
};

test('operator starts with photo/camera actions and a closed detection settings section', () => {
  const html = render(WasteDetector, { apiBase: '', token: '', active: true, onDetections: noop });
  assert.match(html, /Photo \/ camera classification/);
  assert.match(html, /counted automatically by category/);
  assert.match(html, /Add photo/);
  assert.match(html, /Start camera/);
  assert.match(html, /<details class="vision-options"><summary>Detection settings<\/summary>/);
  assert.doesNotMatch(html, /<details[^>]*open/);
  assert.equal(DEFAULT_DETECTION_CONFIDENCE, 0.25);
  assert.match(html, /value="0.25"/);
});

test('strict minimum score explains missed items and offers an explicit default reset', () => {
  const html = render(DetectionSettings, { confidence: 0.95, busy: false, modelStatus: 'best.pt', onConfidence: noop });
  assert.match(html, /Minimum model score: 95%/);
  assert.match(html, /not the model&#x27;s accuracy/);
  assert.match(html, /scores below 95% are hidden/);
  assert.match(html, /Reset to default \(25%\)/);
  assert.match(html, /score below 60%/);
  assert.match(html, /Items below the minimum model score are not detected or counted/);
  const empty = render(DetectionResults, { ...resultsProps, confidence: 0.95, result: { ...resultsProps.result, detections: [] } });
  assert.match(empty, /does not confirm the area is clean/);
  assert.match(empty, /Use default setting and check again/);
  const defaultEmpty = render(DetectionResults, { ...resultsProps, result: { ...resultsProps.result, detections: [] } });
  assert.doesNotMatch(defaultEmpty, /Use default setting and check again/);
});

test('detection results count directly into categories without map placement or physical estimates', () => {
  const html = render(DetectionResults, resultsProps);
  assert.match(html, /Automatically classified/);
  assert.match(html, /Plastic &amp; Cans<\/dt><dd>1<\/dd>/);
  assert.match(html, /Paper &amp; Packaging<\/dt><dd>0<\/dd>/);
  assert.match(html, /Others<\/dt><dd>0<\/dd>/);
  assert.match(html, /Model score: 82%/);
  assert.match(html, /Checking again replaces the counts/);
  assert.match(html, /does not confirm physical collection/);
  assert.doesNotMatch(html, /map|Planning estimates|Weight \(kg\)|Volume \(|Longest side|type="number"/);
});

test('uncertain model labels go to Others while confident supported classes use their categories', () => {
  const detections = [
    observation,
    { detection_id: 'paper', waste_class: 'Paper / Cardboard', confidence: 0.91 },
    { detection_id: 'uncertain-bottle', waste_class: 'Bottle', confidence: 0.35 },
    { detection_id: 'unknown', waste_class: 'CD disc', confidence: 0.9 },
    { detection_id: 'cup', waste_class: 'Cup', confidence: 0.8 },
    { detection_id: 'missing-score', waste_class: 'Can' },
  ];
  const html = render(DetectionResults, { ...resultsProps, result: { ...resultsProps.result, detections } });
  assert.match(html, /Plastic &amp; Cans<\/dt><dd>1<\/dd>/);
  assert.match(html, /Paper &amp; Packaging<\/dt><dd>1<\/dd>/);
  assert.match(html, /Others<\/dt><dd>4<\/dd>/);
  assert.equal((html.match(/Others · unsure/g) || []).length, 3);
  assert.match(html, /Model label: Bottle · Model score: 35%/);
  assert.match(html, /Model score: unavailable/);
  assert.match(html, /5\. Cup/);
});

test('empty successful detection explicitly clears every category', () => {
  const html = render(DetectionResults, { ...resultsProps, result: { ...resultsProps.result, detections: [] } });
  assert.match(html, /No items found in this image/);
  assert.equal((html.match(/<dd>0<\/dd>/g) || []).length, 3);
  assert.doesNotMatch(html, /Confirm|Add.*map|Planning estimates/);
});
