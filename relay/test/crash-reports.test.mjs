import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { RelayStore } from '../src/store.mjs';
import { CrashReports, crashReportsFromEnv, signature, validateReport } from '../src/crash-reports.mjs';

const TOKEN = 'a'.repeat(40);

function diagnostic(offset = 1234) {
  return {
    crashDiagnostics: [{
      diagnosticMetaData: { exceptionType: 1, exceptionCode: 0, signal: 11, terminationReason: 'Namespace SIGNAL, Code 11', appBuildVersion: '236' },
      callStackTree: {
        callStacks: [
          { threadAttributed: false, callStackRootFrames: [{ binaryName: 'libsystem_kernel.dylib', offsetIntoBinaryTextSegment: 1 }] },
          { threadAttributed: true, callStackRootFrames: [{ binaryName: 'Conduit', offsetIntoBinaryTextSegment: offset, subFrames: [{ binaryName: 'SwiftUICore', offsetIntoBinaryTextSegment: 99 }] }] },
        ],
      },
    }],
  };
}

function post(overrides = {}) {
  return { app_version: '2.2.1', build: '236', os: 'iPhone OS 27.2 (24C5)', device: 'iPhone17,2', kind: 'crash', diagnostic: diagnostic(), ...overrides };
}

test('a report needs its build, device and a diagnostic object', () => {
  const report = validateReport(post());
  assert.equal(report.build, '236');
  assert.match(report.signature, /^[0-9a-f]{16}$/);
  assert.throws(() => validateReport(post({ build: '' })), /invalid_build/);
  assert.throws(() => validateReport(post({ device: 'x'.repeat(65) })), /invalid_device/);
  assert.throws(() => validateReport(post({ os: 'iOS\n27' })), /invalid_os/);
  assert.throws(() => validateReport(post({ kind: 'oom' })), /invalid_kind/);
  assert.throws(() => validateReport(post({ diagnostic: [] })), /invalid_diagnostic/);
  assert.throws(() => validateReport(post({ diagnostic: { cpuExceptionDiagnostics: [{}] } })), /invalid_diagnostic/);
  assert.throws(() => validateReport(post({ diagnostic: { crashDiagnostics: {} } })), /invalid_diagnostic/);
  const odd = validateReport(post({ diagnostic: { crashDiagnostics: [{ callStackTree: { callStacks: {} } }] } }));
  assert.match(odd.signature, /^[0-9a-f]{16}$/, 'an odd shape still groups, never a 500');
  const quoted = validateReport(post({ diagnostic: { crashDiagnostics: [{ diagnosticMetaData: { exceptionReason: { composedMessage: 'hi?', arguments: ['hi?'], className: 'NSString' } } }], cpuExceptionDiagnostics: [{}] } }));
  assert.ok(!JSON.stringify(quoted).includes('hi?'), 'the relay drops exception messages itself');
  assert.equal(quoted.diagnostic.cpuExceptionDiagnostics, undefined);
  let deep = { binaryName: 'Conduit' };
  for (let level = 0; level < 5_000; level += 1) deep = { subFrames: [deep] };
  assert.throws(() => validateReport(post({ diagnostic: { crashDiagnostics: [{ callStackTree: { callStacks: [{ callStackRootFrames: [deep] }] } }] } })), /invalid_diagnostic/);
});

test('the signature follows the attributed thread, so one fault groups', () => {
  assert.equal(signature(diagnostic()), signature(diagnostic()));
  assert.notEqual(signature(diagnostic(1234)), signature(diagnostic(5678)));
  assert.equal(signature({}), null);
});

test('reports are kept as private files, newest kept, old ones pruned', () => {
  const dir = mkdtempSync(join(tmpdir(), 'conduit-crash-'));
  try {
    let now = 1_700_000_000_000;
    const reports = new CrashReports({ directory: join(dir, 'crash-reports'), maxReports: 2, maxAgeMs: 10_000, now: () => now });
    const first = reports.save(validateReport(post()));
    now += 1;
    const second = reports.save(validateReport(post({ build: '237' })));
    assert.equal(statSync(join(dir, 'crash-reports', `${first}.report.json`)).mode & 0o777, 0o600);
    assert.deepEqual(reports.list().map((report) => report.id), [first, second]);
    assert.deepEqual(reports.list({ build: '237' }).map((report) => report.id), [second]);
    assert.equal(reports.list()[0].diagnostic, undefined, 'the list carries headings only');
    assert.deepEqual(reports.get(first).diagnostic.crashDiagnostics, diagnostic().crashDiagnostics);
    assert.equal(reports.get('../relay'), null);
    now += 1;
    const third = reports.save(validateReport(post()));
    assert.deepEqual(reports.ids(), [second, third], 'at most maxReports');
    now += 20_000;
    assert.deepEqual(reports.list(), [], 'past maxAgeMs, gone before any new save');
    assert.equal(reports.get(third), null);
    reports.prune();
    assert.deepEqual(reports.ids(), []);
    reports.save(validateReport(post()));
    assert.equal(new CrashReports({ directory: join(dir, 'crash-reports'), now: () => now }).list().length, 1, 'headings load back on restart');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('no token, no reports; a short token is refused', () => {
  assert.equal(crashReportsFromEnv({}, '/tmp/x/relay.json').reports, null);
  assert.throws(() => crashReportsFromEnv({ CRASH_REPORTS_TOKEN: 'short' }, '/tmp/x/relay.json'), /at least 32/);
});

async function startRelay(extraEnv) {
  const dir = mkdtempSync(join(tmpdir(), 'conduit-relay-crash-'));
  const port = 24000 + Math.floor(Math.random() * 1000);
  const keyPath = join(dir, 'apns.pem');
  const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  writeFileSync(keyPath, privateKey.export({ type: 'sec1', format: 'pem' }));
  const created = new RelayStore(join(dir, 'relay.json')).createInstallation({ bundleId: 'com.milim.relay', deviceToken: 'd'.repeat(64), environment: 'production' });
  const device = `${created.installation.id}.${created.deviceSecret}`;
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env, HOST: '127.0.0.1', PORT: String(port), PUBLIC_URL: `https://relay-${port}.example`,
      DATA_PATH: join(dir, 'relay.json'), APNS_KEY_PATH: keyPath, APNS_KEY_ID: 'AAAAAAAAAA', APNS_TEAM_ID: 'BBBBBBBBBB',
      APNS_TOPIC: 'com.milim.relay', APNS_MODE: 'accept', RELAY_TOKEN_PROOF: 'optional', RELAY_TEST_SEAMS: '1', ...extraEnv,
    },
    stdio: 'ignore',
  });
  const base = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 10_000;
  for (;;) {
    try { if ((await fetch(`${base}/healthz`)).ok) break; } catch {}
    if (Date.now() > deadline) throw new Error('relay did not start');
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const api = async (path, { method = 'GET', body, token } = {}) => {
    const response = await fetch(`${base}${path}`, {
      method,
      headers: { ...(body !== undefined ? { 'content-type': 'application/json' } : {}), ...(token ? { authorization: `Bearer ${token}` } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, json: await response.json().catch(() => null) };
  };
  const capabilities = async () => (await api('/v1/meta', { token: device })).json.capabilities;
  return { api, capabilities, dir, stop: () => { child.kill('SIGKILL'); rmSync(dir, { recursive: true, force: true }); } };
}

test('anyone posts a report; only the token reads them', async () => {
  const relay = await startRelay({ CRASH_REPORTS_TOKEN: TOKEN });
  try {
    const sent = await relay.api('/v1/crash-reports', { method: 'POST', body: post() });
    assert.equal(sent.status, 201);
    assert.ok((await relay.capabilities()).includes('crash-reports'), 'the app learns the relay takes reports');
    assert.ok(existsSync(join(relay.dir, 'crash-reports', `${sent.json.id}.report.json`)), 'kept beside the relay data');
    assert.equal((await relay.api('/v1/crash-reports', { method: 'POST', body: post({ kind: 'nope' }) })).status, 400);
    assert.equal((await relay.api('/v1/crash-reports')).status, 401);
    assert.equal((await relay.api('/v1/crash-reports', { token: 'b'.repeat(40) })).status, 401);
    const listed = await relay.api('/v1/crash-reports?build=236', { token: TOKEN });
    assert.deepEqual(listed.json.reports.map((report) => report.id), [sent.json.id]);
    const read = await relay.api(`/v1/crash-reports/${sent.json.id}`, { token: TOKEN });
    assert.equal(read.json.device, 'iPhone17,2');
    assert.deepEqual(read.json.diagnostic.crashDiagnostics, diagnostic().crashDiagnostics);
    assert.equal((await relay.api('/v1/crash-reports', { method: 'DELETE', token: TOKEN })).status, 405);
  } finally {
    relay.stop();
  }
});

test('one address is held to its hourly budget', async () => {
  const relay = await startRelay({ CRASH_REPORTS_TOKEN: TOKEN });
  try {
    const statuses = [];
    for (let index = 0; index < 11; index += 1) {
      statuses.push((await relay.api('/v1/crash-reports', { method: 'POST', body: post() })).status);
    }
    assert.deepEqual(statuses, [...Array(10).fill(201), 429]);
  } finally {
    relay.stop();
  }
});

test('a relay without the token takes no reports', async () => {
  const relay = await startRelay({});
  try {
    assert.equal((await relay.api('/v1/crash-reports', { method: 'POST', body: post() })).status, 404);
    assert.ok(!(await relay.capabilities()).includes('crash-reports'));
    assert.equal((await relay.api('/v1/crash-reports', { token: TOKEN })).status, 404);
    assert.ok(!existsSync(join(relay.dir, 'crash-reports')), 'no reports directory');
  } finally {
    relay.stop();
  }
});
