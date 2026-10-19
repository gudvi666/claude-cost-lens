const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise the shipped page script, with only its startup fetch disabled.
const source = fs.readFileSync(path.join(__dirname, '../app.py'), 'utf8');
const script = source.split('<script>')[1].split('</script>')[0].replace(/loadData\(\);\s*$/, '');
const elements = new Map();
const context = {
  window: { addEventListener() {} },
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, { innerHTML: '', value: '', addEventListener() {} });
      return elements.get(id);
    },
    querySelectorAll() { return []; }
  }
};
vm.createContext(context);
vm.runInContext(script, context);
const labels = row => Array.from(context.sessionModelLabels(row));

test('genuine multi-model primary conversation preserves existing names and order', () => {
  const models = { 'claude-opus-5-5': 2, 'claude-sonnet-5-5': 1 };
  assert.deepEqual(labels({ models, primary_models: models, subagent_models: {} }),
    ['Claude Opus 5.5', 'Claude Sonnet 5.5']);
  assert.deepEqual(labels({ models }), ['Claude Opus 5.5', 'Claude Sonnet 5.5']);
});

test('child model is enclosed in parentheses', () => {
  assert.deepEqual(labels({ primary_models: { 'claude-opus-5-5': 1 },
    subagent_models: { 'claude-haiku-5-5': 2 } }),
    ['Claude Opus 5.5', '(Claude Haiku 5.5)']);
});

test('same model in both roles remains distinguishable', () => {
  assert.deepEqual(labels({ primary_models: { 'claude-opus-5-5': 1 },
    subagent_models: { 'claude-opus-5-5': 2 } }),
    ['Claude Opus 5.5', '(Claude Opus 5.5)']);
});

test('session table renders and escapes role labels and title attributes', () => {
  context.render({
    totals: { tokens: {} }, periods: {}, daily: [], models: [], projects: [],
    active_hours: [], notes: [],
    sessions: [{ title: 'fixture', project: 'fixture', tokens: {},
      primary_models: { 'claude-opus-5-5': 1 },
      subagent_models: { 'claude-haiku-5-5': 1, '<img onerror="alert(1)">': 1 } }]
  });
  const html = elements.get('app').innerHTML;
  assert.match(html, /class="session-models"/);
  assert.match(html, /Claude Opus 5\.5, <span class="subagent-model">\(Claude Haiku 5\.5\)<\/span>/);
  assert.match(html, /<span class="subagent-model">\(&lt;img Onerror=&quot;alert\(1\)&quot;&gt;\)<\/span>/);
  assert(!html.includes('<img'));
});
