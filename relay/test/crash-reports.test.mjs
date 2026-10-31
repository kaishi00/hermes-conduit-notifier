import { strict as assert } from 'node:assert';
import { generateKeyPairSync } from 'node:crypto';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { RelayStore } from '../src/store.mjs';
import { CrashReports, MAX_FRAME_LEVELS, MAX_TRACE_LINES, MAX_TRACE_LINE_CHARS, crashReportRoutes, crashReportsFromEnv, signature, validateReport } from '../src/crash-reports.mjs';

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

// Lines as Conduit's ChatViewportTrace writes them: seconds before the save,
// then the event.
function trace(reason = 'jump', lines = ['-2.104 surface setOffset 5120.0 -> 5180.0 animated 0 (content 6000.0, bounds 820.0)', '-0.412 jump 5180.0 -> 0.0 (no drag, no command); mode following', '-0.400 blank viewport: offset 0.0 content 6000.0, 0 rows laid out; rebuilding the scroll view']) {
  return { reason, lines };
}

function chatPost(overrides = {}) {
  const { diagnostic: _, ...fields } = post({ kind: 'chat', trace: trace() });
  return { ...fields, ...overrides };
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
  const quoted = validateReport(post({ diagnostic: { crashDiagnostics: [{ diagnosticMetaData: { exceptionReason: { composedMessage: 'hi?', arguments: ['hi?'], formatString: 'hi? %@', className: 'NSString' } } }], cpuExceptionDiagnostics: [{}] } }));
  assert.ok(!JSON.stringify(quoted).includes('hi?'), 'the relay drops exception messages itself');
  assert.equal(quoted.diagnostic.cpuExceptionDiagnostics, undefined);
  const extra = validateReport(post({ diagnostic: { crashDiagnostics: [{ diagnosticMetaData: { pid: 42, regionFormat: 'NZ', signal: 11, osVersion: { nested: 'x' } }, callStackTree: { callStacks: [{ callStackRootFrames: [{ binaryName: 'Conduit', symbol: 'secret' }] }] } }] } }));
  assert.deepEqual(extra.diagnostic.crashDiagnostics[0].diagnosticMetaData, { signal: 11 }, 'only named keys, and no nested values');
  assert.ok(!JSON.stringify(extra).includes('secret'));
  const wide = { crashDiagnostics: [{ callStackTree: { callStacks: [{ callStackRootFrames: [{ binaryName: '€'.repeat(300_000) }] }] } }] };
  assert.throws(() => validateReport(post({ diagnostic: wide })), /payload_too_large/, 'the cap counts bytes');
  const nested = (levels) => {
    let frame = { binaryName: 'Conduit' };
    for (let level = 0; level < levels; level += 1) frame = { subFrames: [frame] };
    return { crashDiagnostics: [{ callStackTree: { callStacks: [{ callStackRootFrames: [frame] }] } }] };
  };
  const levels = (report) => {
    let count = 0;
    let frames = report.diagnostic.crashDiagnostics[0].callStackTree.callStacks[0].callStackRootFrames;
    while (frames?.length) {
      count += 1;
      frames = frames[0].subFrames;
    }
    return count;
  };
  assert.equal(levels(validateReport(post({ diagnostic: nested(255) }))), 256, 'as deep as Conduit sends, whole');
  assert.equal(levels(validateReport(post({ diagnostic: nested(5_000) }))), MAX_FRAME_LEVELS, 'an overflowed stack keeps its first levels');
});

test('a chat trace comes alone or with a crash, in the characters the app writes', () => {
  const chat = validateReport(chatPost());
  assert.equal(chat.kind, 'chat');
  assert.deepEqual(chat.trace, trace());
  assert.equal(chat.diagnostic, undefined, 'a chat report has no diagnostic');
  assert.match(chat.signature, /^[0-9a-f]{16}$/);
  assert.equal(validateReport(chatPost({ trace: trace('jump', ['-1.000 other']) })).signature, chat.signature, 'traces group by what saved them');
  assert.notEqual(validateReport(chatPost({ trace: trace('stall') })).signature, chat.signature);
  assert.throws(() => validateReport(chatPost({ trace: undefined })), /invalid_trace/, 'a chat report is its trace');
  assert.throws(() => validateReport(chatPost({ trace: trace('typed') })), /invalid_trace/);
  assert.throws(() => validateReport(chatPost({ trace: trace('jump', []) })), /invalid_trace/);
  assert.throws(() => validateReport(chatPost({ trace: trace('jump', ['ok', 7]) })), /invalid_trace/);
  assert.throws(() => validateReport(chatPost({ trace: trace('jump', Array(MAX_TRACE_LINES + 1).fill('x')) })), /invalid_trace/);
  assert.throws(() => validateReport(chatPost({ trace: trace('jump', ['x'.repeat(MAX_TRACE_LINE_CHARS + 1)]) })), /invalid_trace/);
  for (const text of ['Hi, I said "yes"', 'naïve', 'two\nlines', 'tab\tthen', 'smile 🙂']) {
    assert.throws(() => validateReport(chatPost({ trace: trace('manual', [text]) })), /invalid_trace/, `refuses ${JSON.stringify(text)}`);
  }
  assert.equal(validateReport(chatPost({ trace: trace('manual', Array(MAX_TRACE_LINES).fill('y'.repeat(MAX_TRACE_LINE_CHARS))) })).trace.lines.length, MAX_TRACE_LINES, 'a full trace');
  const crash = validateReport(post({ trace: trace('stall') }));
  assert.deepEqual(crash.trace, trace('stall'), 'a crash or hang can carry the trace saved before it');
  assert.equal(crash.signature, validateReport(post()).signature, 'the trace leaves the grouping alone');
  assert.equal(validateReport(post()).trace, undefined);
  assert.throws(() => validateReport(post({ trace: 'lines' })), /invalid_trace/);
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
    now += 1;
    const traced = reports.save(validateReport(chatPost()));
    const heading = reports.list().find((report) => report.id === traced);
    assert.equal(heading.trace_reason, 'jump');
    assert.equal(heading.trace, undefined, 'the list carries no trace lines');
    assert.deepEqual(reports.get(traced).trace, trace());
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('only a valid post spends the daily budget', async () => {
  const spent = [];
  const routes = crashReportRoutes({
    reports: { save: () => 'id' },
    adminToken: TOKEN,
    readJson: async (request) => request.body,
    enforceRateLimit: (key) => spent.push(key.split(':')[0]),
    clientAddress: () => '203.0.113.1',
  });
  const send = (body) => routes({ method: 'POST', body, headers: {} }, {}, new URL('http://relay/v1/crash-reports'), () => {});
  await assert.rejects(send(post({ kind: 'nope' })), /invalid_kind/);
  assert.deepEqual(spent, ['crash-report'], 'refused before the daily budget');
  await send(post());
  assert.deepEqual(spent, ['crash-report', 'crash-report', 'crash-report-global']);
});

test('a heading without its report is dropped on start', () => {
  const dir = mkdtempSync(join(tmpdir(), 'conduit-crash-'));
  try {
    const directory = join(dir, 'crash-reports');
    const reports = new CrashReports({ directory });
    const id = reports.save(validateReport(post()));
    rmSync(join(directory, `${id}.report.json`));
    new CrashReports({ directory });
    assert.ok(!existsSync(join(directory, `${id}.head.json`)));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('no token or a short one, no reports', () => {
  assert.equal(crashReportsFromEnv({}, '/tmp/x/relay.json').reports, null);
  assert.equal(crashReportsFromEnv({ CRASH_REPORTS_TOKEN: 'short' }, '/tmp/x/relay.json').reports, null, 'a short token leaves reports off, the relay up');
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
    assert.equal(sent.json.trace, undefined, 'no trace sent, none kept');
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

test('a chat trace posts alone, and a big diagnostic still takes a full trace', async () => {
  const relay = await startRelay({ CRASH_REPORTS_TOKEN: TOKEN });
  try {
    const chat = await relay.api('/v1/crash-reports', { method: 'POST', body: chatPost() });
    assert.equal(chat.status, 201);
    assert.equal(chat.json.trace, true, 'says it kept the trace, so the app lets its copy go');
    const read = await relay.api(`/v1/crash-reports/${chat.json.id}`, { token: TOKEN });
    assert.equal(read.json.kind, 'chat');
    assert.deepEqual(read.json.trace, trace());
    const wide = diagnostic();
    wide.crashDiagnostics[0].callStackTree.callStacks[1].callStackRootFrames[0].binaryName = 'C'.repeat(500_000);
    const full = trace('stall', Array(MAX_TRACE_LINES).fill('z'.repeat(MAX_TRACE_LINE_CHARS)));
    assert.equal((await relay.api('/v1/crash-reports', { method: 'POST', body: post({ diagnostic: wide, trace: full }) })).status, 201);
    assert.equal((await relay.api('/v1/crash-reports', { method: 'POST', body: chatPost({ trace: trace('manual', ['quote "x"']) }) })).status, 400);
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
