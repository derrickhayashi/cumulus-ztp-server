const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');

function app() {
  let factory;
  const context = {
    document: { hidden: false, addEventListener: (_, callback) => callback() },
    Alpine: { data: (_, callback) => { factory = callback; } },
    AbortSignal, setInterval, clearInterval, Date,
  };
  vm.runInNewContext(source, context);
  return factory();
}

const status = (stage, result, extra = {}) => ({hostname: 'leaf01', stage, result, ...extra});

test('only a final completion report means completed', () => {
  const ui = app();
  ui.progressLoaded = true;
  assert.equal(ui.progressLabel(null), 'No reports yet');
  assert.equal(ui.progressLabel(status('config_apply', 'succeeded')), 'In progress');
  assert.equal(ui.progressLabel(status('provisioning', 'succeeded')), 'Completed');
  assert.equal(ui.stageLabel('provisioning'), 'Configuration saved');
  assert.equal(ui.progressLabel(status('os_install', 'failed')), 'Failed');
  assert.equal(ui.progressLabel(status('reboot', 'requested')), 'Reboot requested');
  assert.equal(ui.progressLabel(status('configuration_payload', 'requested')), 'Awaiting switch reports');
  assert.equal(ui.progressFor('constructor'), null);
});

test('fetches every page of statuses and preserves old data after an outage', async () => {
  const ui = app();
  const calls = [];
  ui.api = async url => {
    calls.push(url);
    return {devices: url.endsWith('offset=0')
      ? Array.from({length: 1000}, (_, index) => status('config_save', 'succeeded', {hostname: `leaf${index}`}))
      : [status('provisioning', 'succeeded', {hostname: 'last-leaf'})]};
  };
  await ui.refreshProgress();
  assert.equal(calls.length, 2);
  assert.equal(Object.keys(ui.progress).length, 1001);
  const updated = ui.progressUpdated;
  ui.api = async () => { throw new Error('Storage unavailable'); };
  await ui.refreshProgress();
  assert.equal(ui.progressUpdated, updated);
  assert.equal(ui.progressFor('last-leaf').stage, 'provisioning');
  assert.match(ui.progressError, /Showing last received statuses/);
  assert.equal(ui.progressBusy, false);
});

test('a failed progress request does not block inventory loading', async () => {
  const ui = app();
  ui.api = async url => {
    if (url.startsWith('/api/v1/provisioning')) throw new Error('Unavailable');
    return {devices: [{hostname: 'leaf01'}], networks: {}, revision: 'revision'};
  };
  await ui.load();
  assert.equal(ui.devices[0].hostname, 'leaf01');
  assert.equal(ui.error, '');
  assert.match(ui.progressError, /No progress status could be loaded/);
  assert.equal(ui.progressLabel(null), 'Unavailable');
});

test('overlapping polling requests are skipped', async () => {
  const ui = app();
  let resolve;
  let count = 0;
  ui.api = () => { count += 1; return new Promise(callback => { resolve = callback; }); };
  const pending = ui.refreshProgress();
  await ui.refreshProgress();
  assert.equal(count, 1);
  resolve({devices: []});
  await pending;
  assert.equal(ui.progressLoaded, true);
});

test('history paging retains unique events and handles a late response after closing', async () => {
  const ui = app();
  const urls = [];
  ui.api = async url => {
    urls.push(url);
    return {events: url.endsWith('offset=0') ? Array.from({length: 100}, (_, id) => ({id})) : [{id: 99}, {id: 100}]};
  };
  await ui.openHistory({hostname: 'leaf 01'});
  assert.equal(ui.historyMore, true);
  await ui.loadHistory();
  assert.match(urls[0], /leaf%2001/);
  assert.match(urls[1], /offset=100/);
  assert.equal(ui.historyEvents.length, 101);
  assert.equal(ui.historyMore, false);
  let resolve;
  ui.api = () => new Promise(callback => { resolve = callback; });
  const pending = ui.openHistory({hostname: 'leaf02'});
  ui.closeHistory();
  resolve({events: [{id: 999}]});
  await pending;
  assert.equal(ui.historyHostname, null);
  assert.equal(ui.historyEvents.length, 0);
  assert.equal(ui.historyLoading, false);
});


test('OS override survives editing and clearing restores inheritance', async () => {
  const ui = app();
  ui.$nextTick = () => {};
  ui.openEdit({hostname: 'leaf01', serial: 'LEAF', target_os: '5.18.4'});
  assert.equal(ui.payload().target_os, '5.18.4');
  ui.form.target_os = '';
  assert.equal(ui.payload().target_os, null);
  ui.openCreate();
  assert.equal(ui.payload().target_os, null);
  ui.api = async url => url.startsWith('/api/v1/provisioning')
    ? {devices: []}
    : {devices: [], networks: {}, revision: 'revision', settings: {target_os: '5.18.3'}};
  await ui.load();
  assert.equal(ui.defaultTargetOs, '5.18.3');
});

test('sorting uses numeric IP and hostname order, keeps missing values last, and preserves inventory order', () => {
  const ui = app();
  ui.devices = [
    {hostname: 'leaf10', mgmt_ip: '192.0.2.100'},
    {hostname: 'leaf2', mgmt_ip: '192.0.2.2'},
    {hostname: 'leaf1'},
    {hostname: 'leaf3', mgmt_ip: '192.0.2.10'},
  ];
  const names = () => Array.from(ui.filteredDevices, device => device.hostname);
  assert.deepEqual(names(), ['leaf1', 'leaf2', 'leaf3', 'leaf10']);
  ui.sortBy('mgmt_ip');
  assert.deepEqual(names(), ['leaf2', 'leaf3', 'leaf10', 'leaf1']);
  ui.sortBy('mgmt_ip');
  assert.deepEqual(names(), ['leaf10', 'leaf3', 'leaf2', 'leaf1']);
  assert.deepEqual(ui.devices.map(device => device.hostname), ['leaf10', 'leaf2', 'leaf1', 'leaf3']);
  ui.query = 'leaf1';
  assert.deepEqual(names(), ['leaf10', 'leaf1']);
  ui.sortBy('hostname');
  assert.equal(ui.sortDirection, 'asc');
  assert.deepEqual(names(), ['leaf1', 'leaf10']);
});

test('MAC and serial sorting toggle direction and resolve duplicate values by hostname', () => {
  const ui = app();
  ui.devices = [
    {hostname: 'leaf10', eth0_mac: 'aa:00:00:00:00:10', serial: 'SN2'},
    {hostname: 'leaf2', eth0_mac: 'aa:00:00:00:00:02', serial: 'SN2'},
    {hostname: 'leaf1', eth0_mac: 'aa:00:00:00:00:01', serial: 'SN10'},
  ];
  const names = () => Array.from(ui.filteredDevices, device => device.hostname);
  ui.sortBy('eth0_mac');
  assert.deepEqual(names(), ['leaf1', 'leaf2', 'leaf10']);
  ui.sortBy('eth0_mac');
  assert.deepEqual(names(), ['leaf10', 'leaf2', 'leaf1']);
  ui.sortBy('serial');
  assert.deepEqual(names(), ['leaf2', 'leaf10', 'leaf1']);
});

test('provisioning sorts by displayed status and responds to refreshed progress', () => {
  const ui = app();
  ui.progressLoaded = true;
  ui.devices = [{hostname: 'leaf1'}, {hostname: 'leaf2'}, {hostname: 'leaf3'}];
  ui.progress = {
    leaf1: status('os_install', 'failed'),
    leaf3: status('provisioning', 'succeeded'),
  };
  ui.sortBy('provisioning');
  const names = () => Array.from(ui.filteredDevices, device => device.hostname);
  assert.deepEqual(names(), ['leaf3', 'leaf1', 'leaf2']);
  ui.progress.leaf1 = status('config_apply', 'started');
  assert.deepEqual(names(), ['leaf3', 'leaf1', 'leaf2']);
  ui.sortBy('provisioning');
  assert.deepEqual(names(), ['leaf2', 'leaf1', 'leaf3']);
});
