import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const script = fs.readFileSync(new URL('../loading/app.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function setup() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { hidden: false, textContent: '', value: 0,
      scrollHeight: 100, scrollTop: 0, clientHeight: 100,
      addEventListener(type, callback) { this[type] = callback; } });
    return elements.get(id);
  };
  let resolveInstall, rejectInstall, interval, snapshot;
  const context = vm.createContext({
    document: { getElementById: element, querySelector: element },
    window: { __TAURI__: { core: { invoke: async (command, args) => {
      if (command === 'get_install_progress') return snapshot;
      if (!args.install) return 'missing';
      return new Promise((resolve, reject) => { resolveInstall = resolve; rejectInstall = reject; });
    } } } },
    setInterval: callback => { interval = callback; return 1; },
    clearInterval: () => { interval = undefined; }, Date,
  });
  vm.runInContext(script, context);
  return { element, tick: () => interval?.(), active: () => Boolean(interval),
    snapshot: value => { snapshot = value; }, resolve: value => resolveInstall(value),
    reject: value => rejectInstall(value) };
}

test('progress and log follow real snapshots; completion stops polling', async () => {
  const ui = setup();
  await flush();
  ui.element('install').click();
  ui.snapshot({ completed: 6, label: 'Tải model', log: '\x1b[32mDownloading\x1b[0m\r10 MB' });
  ui.tick(); await flush();
  assert.equal(ui.element('install-progress').value, 6);
  assert.equal(ui.element('progress-count').textContent, '6/8 bước hoàn tất');
  assert.equal(ui.element('install-log').textContent, 'Downloading\n10 MB');
  ui.snapshot({ completed: 8, label: 'Hoàn tất', log: 'Done' });
  ui.resolve('ready'); await flush();
  assert.equal(ui.element('install-progress').value, 8);
  assert.equal(ui.active(), false);
});

test('failure retains logs, offers retry and does not mark progress complete', async () => {
  const ui = setup(); await flush();
  ui.element('install').click();
  ui.snapshot({ completed: 4, label: 'LatentSync', log: 'Download failed' });
  ui.reject(new Error('setup failed')); await flush();
  assert.equal(ui.element('install-progress').value, 4);
  assert.equal(ui.element('install-log').textContent, 'Download failed');
  assert.equal(ui.element('install').hidden, false);
  assert.equal(ui.active(), false);
  ui.element('install').click();
  assert.equal(ui.element('install-progress').value, 0);
  assert.equal(ui.element('install-log').textContent, 'Đang chờ nhật ký…');
  ui.resolve('reboot'); await flush();
  assert.match(ui.element('stage').textContent, /khởi động lại máy/);
  assert.equal(ui.active(), false);
});

test('large logs are bounded and HTML remains plain text', async () => {
  const ui = setup(); await flush();
  ui.element('install').click();
  ui.snapshot({ completed: 99, label: 'Done', log: Array(500).fill('<b>log</b>').join('\n') });
  ui.tick(); await flush();
  assert.equal(ui.element('install-log').textContent.split('\n').length, 300);
  assert.equal(ui.element('install-progress').value, 8);
  assert.match(ui.element('install-log').textContent, /<b>log<\/b>/);
  ui.resolve('ready'); await flush();
});
