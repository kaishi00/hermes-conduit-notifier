import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, test } from 'node:test';

const commandPath = fileURLToPath(new URL('../src/storage-status.mjs', import.meta.url));
const directory = mkdtempSync(join(tmpdir(), 'conduit-storage-status-'));
const dataPath = join(directory, 'relay-data.json');
after(() => rmSync(directory, { recursive: true, force: true }));

test('storage status reports only installation counts and never changes the data file', () => {
  const data = {
    version: 1,
    installations: {
      active: { active: true, deviceToken: 'DEVICE_TOKEN_MUST_NOT_PRINT', deviceSecretHash: 'DEVICE_DIGEST_MUST_NOT_PRINT' },
      inactive: { active: false, gateways: { gateway: { secretHash: 'GATEWAY_DIGEST_MUST_NOT_PRINT' } } },
      legacy: { gateways: {} },
    },
    pairings: { codeHash: 'PAIRING_HASH_MUST_NOT_PRINT' },
    eventIds: { id: Date.now() },
    pendingDecisions: { id: { answer: 'PRIVATE_ANSWER_MUST_NOT_PRINT' } },
  };
  writeFileSync(dataPath, `${JSON.stringify(data)}\n`, { encoding: 'utf8', mode: 0o600 });
  const before = readFileSync(dataPath, 'utf8');

  const explicitPath = spawnSync(process.execPath, [commandPath, dataPath], { encoding: 'utf8' });
  assert.equal(explicitPath.status, 0, explicitPath.stderr);
  assert.deepEqual(JSON.parse(explicitPath.stdout), {
    installations: { total: 3, active: 1, inactive: 2, capacity: 50_000 },
  });
  assert.equal(explicitPath.stderr, '');

  const configured = spawnSync(process.execPath, [commandPath, dataPath], {
    encoding: 'utf8', env: { ...process.env, RELAY_MAX_INSTALLATIONS: '1024' },
  });
  assert.equal(configured.status, 0, configured.stderr);
  assert.equal(JSON.parse(configured.stdout).installations.capacity, 1_024, 'capacity follows the relay setting');

  const misconfigured = spawnSync(process.execPath, [commandPath, dataPath], {
    encoding: 'utf8', env: { ...process.env, RELAY_MAX_INSTALLATIONS: '10k' },
  });
  assert.equal(misconfigured.status, 1);
  assert.equal(misconfigured.stdout, '');
  assert.equal(misconfigured.stderr, 'RELAY_MAX_INSTALLATIONS must be a positive integer.\nstorage_status_unavailable\n',
    'a bad setting is named, as at relay boot');

  const envPath = spawnSync(process.execPath, [commandPath], {
    encoding: 'utf8', env: { ...process.env, DATA_PATH: dataPath },
  });
  assert.equal(envPath.status, 0, envPath.stderr);
  assert.equal(envPath.stdout, explicitPath.stdout);
  assert.equal(envPath.stderr, '');
  for (const privateValue of Object.values(data.installations.active)) {
    if (typeof privateValue === 'string') {
      assert.equal(explicitPath.stdout.includes(privateValue), false);
      assert.equal(explicitPath.stderr.includes(privateValue), false);
    }
  }
  assert.doesNotMatch(explicitPath.stdout + explicitPath.stderr, /MUST_NOT_PRINT|PRIVATE_ANSWER/);
  assert.equal(readFileSync(dataPath, 'utf8'), before, 'status inspection is read-only');

  writeFileSync(dataPath, 'MALFORMED_PRIVATE_STORE_CONTENT');
  const malformed = spawnSync(process.execPath, [commandPath, dataPath], { encoding: 'utf8' });
  assert.equal(malformed.status, 1);
  assert.equal(malformed.stdout, '');
  assert.equal(malformed.stderr, 'storage_status_unavailable\n');
  assert.doesNotMatch(malformed.stdout + malformed.stderr, /MALFORMED_PRIVATE_STORE_CONTENT/);
});
