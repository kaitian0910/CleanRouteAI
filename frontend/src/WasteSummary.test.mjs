import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
function load(file, dependencies = {}) {
  const { outputText } = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  });
  const exports = {};
  new Function('require', 'exports', outputText)(name => dependencies[name] || require(name), exports);
  return exports;
}
const classifier = load('./visionClassification.ts');
const { default: WasteSummary } = load('./WasteSummary.tsx', { './visionClassification': classifier });
const completedCollections = { plasticAndCans: 100, paperAndPackaging: 100, others: 100,
  binsEmptied: 3, bulkyItems: 2, totalWeightKg: 12 };
const render = observations => renderToStaticMarkup(React.createElement(WasteSummary, { observations, completedCollections }));
const observation = (id, label, confidence) => ({ detection_id: id, waste_class: label, confidence });

test('vision cards count the current detections directly without adding legacy collection categories', () => {
  const html = render([
    observation('a', 'Bottle', .9), observation('b', 'Paper & Cardboard', .9),
    observation('c', 'Can', .4), observation('d', 'Unrecognized material', .95),
    observation('e', 'Cup', .99), observation('a', 'Bottle', .9),
  ]);
  assert.match(html, /5 detected items/);
  assert.match(html, /waste-cell-plastic[\s\S]*?1 items/);
  assert.match(html, /waste-cell-paper[\s\S]*?1 items/);
  assert.match(html, /waste-cell-others[\s\S]*?3 items/);
  assert.match(html, /2 uncertain items are included in Others/);
  assert.doesNotMatch(html, /100 items|50 g|map location/);
});

test('empty image counts stay zero while completed pickups and their estimate remain separate', () => {
  const html = render([]);
  assert.match(html, /0 detected items/);
  assert.equal((html.match(/0 items/g) || []).length, 3);
  assert.match(html, /Standard bins emptied: <b>3<\/b>/);
  assert.match(html, /Bulky items: <b>2<\/b>/);
  assert.match(html, /Estimated collected weight: <b>12 kg<\/b>/);
  assert.match(html, /does not confirm physical collection or measure weight/);
});

test('the next snapshot replaces the previous counts rather than keeping accumulated totals', () => {
  assert.match(render([observation('a', 'Bottle', .9), observation('b', 'Can', .9)]), /2 detected items/);
  const replaced = render([observation('next', 'Bottle', .59)]);
  assert.match(replaced, /1 detected items/);
  assert.match(replaced, /waste-cell-plastic[\s\S]*?0 items/);
  assert.match(replaced, /waste-cell-others[\s\S]*?1 items/);
  assert.match(replaced, /1 uncertain item is included in Others/);
});
