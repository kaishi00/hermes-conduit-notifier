import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { after, test } from 'node:test';

const commandPath = fileURLToPath(new URL('../src/storage-prune-inactive.mjs', import.meta.url));
const directory = mkdtempSync(join(tmpdir(), 'conduit-storage-prune-'));
after(() => rmSync(directory, { recursive: true, force: true }));

function installation(active, updatedAt, fields = {}) {
  return { active, updatedAt, ...fields };
}

function run(dataPath, args = []) {
  return spawnSync(process.execPath, [commandPath, dataPath, ...args], {
    encoding: 'utf8', env: { ...process.env, DATA_PATH: undefined },
  });
}

test('dry-run preserves bytes and apply removes only eligible installations with an exact backup', () => {
  const dataPath = join(directory, 'relay-data.json');
  const old = new Date(Date.now() - 31 * 24 * 60 * 60_000).toISOString();
  const recent = new Date(Date.now() - 29 * 24 * 60 * 60_000).toISOString();
  const data = {
    version: 1,
    installations: {
      old_gatewayless: installation(false, old, { gateways: {}, deviceToken: 'PRIVATE_DEVICE_TOKEN' }),
      old_no_gateways: installation(false, old),
      active: installation(true, old),
      recent: installation(false, recent),
      has_gateway: installation(false, old, { gateways: { gw: { id: 'PRIVATE_GATEWAY_ID' } } }),
      legacy_gateway_secret: installation(false, old, { gatewaySecretHash: 'PRIVATE_LEGACY_DIGEST' }),
      paired: installation(false, old),
      pending: installation(false, old),
      future: installation(false, new Date(Date.now() + 60_000).toISOString()),
      invalid_time: installation(false, 'not-a-date'),
      missing_time: { active: false, gateways: {} },
      legacy_null: installation(false, old, { gatewaySecretHash: null }),
    },
    pairings: { expired_pairing: { installationId: 'paired', expiresAt: '2000-01-01T00:00:00.000Z' } },
    pendingDecisions: { expired_decision: { installationId: 'pending', gatewayId: 'gw', createdAt: 1 } },
    eventIds: { 'old_gatewayless:gw:accepted': Date.now() },
  };
  writeFileSync(dataPath, `${JSON.stringify(data)}\n`, { encoding: 'utf8', mode: 0o600 });
  const original = readFileSync(dataPath);

  const preview = run(dataPath);
  assert.equal(preview.status, 0, preview.stderr);
  assert.deepEqual(JSON.parse(preview.stdout), { eligible: 2, removed: 0, remaining: 12 });
  assert.equal(preview.stderr, '');
  assert.deepEqual(readFileSync(dataPath), original, 'dry-run never changes the store');
  assert.deepEqual(readdirSync(directory), ['relay-data.json'], 'dry-run creates no backup or temporary file');

  const missingAcknowledgement = run(dataPath, ['--apply']);
  assert.equal(missingAcknowledgement.status, 1);
  assert.equal(missingAcknowledgement.stdout, '');
  assert.equal(missingAcknowledgement.stderr, 'storage_prune_unavailable\n');
  assert.deepEqual(readFileSync(dataPath), original, 'apply without stopped-relay acknowledgement is rejected before writes');

  const applied = run(dataPath, ['--apply', '--relay-stopped']);
  assert.equal(applied.status, 0, applied.stderr);
  assert.deepEqual(JSON.parse(applied.stdout), { eligible: 2, removed: 2, remaining: 10 });
  assert.equal(applied.stderr, '');
  const backups = readdirSync(directory).filter((name) => name.startsWith('relay-data.json.backup-'));
  assert.equal(backups.length, 1);
  assert.deepEqual(readFileSync(join(directory, backups[0])), original, 'backup preserves the exact original bytes');
  if (process.platform !== 'win32') {
    assert.equal(statSync(join(directory, backups[0])).mode & 0o777, 0o600);
    assert.equal(statSync(dataPath).mode & 0o777, 0o600);
  }
  const saved = JSON.parse(readFileSync(dataPath, 'utf8'));
  const remaining = saved.installations;
  assert.deepEqual(Object.keys(remaining).sort(), ['active', 'future', 'has_gateway', 'invalid_time', 'legacy_gateway_secret', 'legacy_null', 'missing_time', 'paired', 'pending', 'recent']);
  const expected = structuredClone(data);
  delete expected.installations.old_gatewayless;
  delete expected.installations.old_no_gateways;
  assert.deepEqual(saved, expected, 'only eligible installation entries change; pairing, decision, and dedupe records survive');
  assert.doesNotMatch(applied.stdout + applied.stderr, /PRIVATE_DEVICE_TOKEN|PRIVATE_GATEWAY_ID|PRIVATE_LEGACY_DIGEST/);
});

test('malformed maps and reference records fail closed without disclosing contents', () => {
  const malformedCases = [
    { version: 1, installations: [], pairings: {}, pendingDecisions: {}, eventIds: {} },
    { version: 1, installations: { active: null }, pairings: {}, pendingDecisions: {}, eventIds: {} },
    { version: 1, installations: { gatewayRecord: { gateways: { bad: null } }, eligible: installation(false, new Date(0).toISOString()) }, pairings: {}, pendingDecisions: {}, eventIds: {} },
    { version: 1, installations: {}, pairings: { bad: { privateValue: 'PRIVATE_PAIRING' } }, pendingDecisions: {}, eventIds: {} },
    { version: 1, installations: {}, pairings: {}, pendingDecisions: { bad: { answer: 'PRIVATE_ANSWER' } }, eventIds: {} },
  ];
  for (let index = 0; index < malformedCases.length; index += 1) {
    const dataPath = join(directory, `malformed-${index}.json`);
    const bytes = Buffer.from(`${JSON.stringify(malformedCases[index])}\n`);
    writeFileSync(dataPath, bytes, { mode: 0o600 });
    const result = run(dataPath, ['--apply', '--relay-stopped']);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.equal(result.stderr, 'storage_prune_unavailable\n');
    assert.deepEqual(readFileSync(dataPath), bytes, 'malformed state is never rewritten');
    assert.doesNotMatch(result.stdout + result.stderr, /PRIVATE_PAIRING|PRIVATE_ANSWER/);
  }
});

test('applying with no eligible installations creates neither backup nor replacement', () => {
  const dataPath = join(directory, 'nothing-eligible.json');
  const bytes = Buffer.from(`${JSON.stringify({
    version: 1,
    installations: { active: installation(true, new Date(0).toISOString()) },
    pairings: {}, pendingDecisions: {}, eventIds: {},
  })}\n`);
  writeFileSync(dataPath, bytes, { mode: 0o600 });

  const result = run(dataPath, ['--apply', '--relay-stopped']);

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { eligible: 0, removed: 0, remaining: 1 });
  assert.deepEqual(readFileSync(dataPath), bytes);
  assert.equal(readdirSync(directory).some((name) => name.startsWith('nothing-eligible.json.backup-')), false);
});

test('a file with a null eventIds field is accepted, as the relay accepts it', () => {
  const dataPath = join(directory, 'null-event-ids.json');
  writeFileSync(dataPath, `${JSON.stringify({
    version: 1,
    installations: { active: installation(true, new Date(0).toISOString()) },
    pairings: {}, pendingDecisions: {}, eventIds: null,
  })}\n`, { mode: 0o600 });

  const result = run(dataPath, []);

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { eligible: 0, removed: 0, remaining: 1 });
});

test('a changed original is preserved and the replacement temporary file is removed', () => {
  const dataPath = join(directory, 'concurrent-change.json');
  const preloadPath = join(directory, 'concurrent-writer.mjs');
  const original = Buffer.from(`${JSON.stringify({
    version: 1, installations: { eligible: installation(false, new Date(0).toISOString()) },
    pairings: {}, pendingDecisions: {}, eventIds: {},
  })}\n`);
  writeFileSync(dataPath, original, { mode: 0o600 });
  // Inject a deterministic competing write at the byte recheck, without
  // adding a production test hook or depending on timing a real race.
  writeFileSync(preloadPath, `
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
const read = fs.readFileSync;
let reads = 0;
fs.readFileSync = function(path, ...args) {
  if (path === process.env.TEST_STORE && ++reads === 2) fs.writeFileSync(path, 'CONCURRENT_STORE_BYTES');
  return read.call(this, path, ...args);
};
syncBuiltinESMExports();
`);
  const result = spawnSync(process.execPath, ['--import', pathToFileURL(preloadPath).href, commandPath, dataPath, '--apply', '--relay-stopped'], {
    encoding: 'utf8', env: { ...process.env, TEST_STORE: dataPath },
  });
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, 'storage_prune_unavailable\n');
  assert.equal(readFileSync(dataPath, 'utf8'), 'CONCURRENT_STORE_BYTES', 'never replace the competing writer bytes');
  const backups = readdirSync(directory).filter((name) => name.startsWith('concurrent-change.json.backup-'));
  assert.equal(backups.length, 1);
  assert.deepEqual(readFileSync(join(directory, backups[0])), original);
  assert.equal(readdirSync(directory).some((name) => name.startsWith('concurrent-change.json.prune-')), false);
});
