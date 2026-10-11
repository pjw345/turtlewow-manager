const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const elements = new Map();
function element() {
  return {style: {}, dataset: {}, classList: {add() {}, remove() {}},
    lastChild: {}, addEventListener() {}, removeAttribute() {},
    replaceChildren() {}, append() {}, setAttribute() {}};
}
const build = element();
build.dataset.action = 'build';
const context = vm.createContext({
  document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    querySelectorAll() { return [build]; },
    createElement: element, createTextNode: value => value, body: element()
  },
  fetch: () => Promise.resolve({json: () => Promise.resolve({authenticated: false})}),
  setInterval() {}, setTimeout() {}, Date
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);
for (const [state, disabled] of [
  [{}, true],
  [{build_available: false, build_reason: 'Already current'}, true],
  [{build_available: true}, false],
  [{build_available: true, busy: true}, true],
  [{build_available: true, error: 'Connection failed'}, true]
]) {
  context.sample = state;
  vm.runInContext('render(sample)', context);
  assert.equal(build.disabled, disabled, JSON.stringify(state));
}
assert.match(fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8'), /data-action="build" disabled/);
console.log('Build controls: current, update available, unknown, busy and failure states passed.');
