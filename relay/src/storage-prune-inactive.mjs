import { randomUUID } from 'node:crypto';
import { closeSync, fsyncSync, lstatSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';

const retentionMs = 30 * 24 * 60 * 60_000;
const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

function writeExclusive(path, bytes) {
  const fd = openSync(path, 'wx', 0o600);
  let complete = false;
  try {
    writeFileSync(fd, bytes);
    fsyncSync(fd);
    complete = true;
  } finally {
    closeSync(fd);
    if (!complete) {
      try { unlinkSync(path); } catch { /* Preserve the original failure. */ }
    }
  }
}

function main() {
  let temporary;
  try {
    const args = process.argv.slice(2);
    const flags = args.filter((arg) => arg.startsWith('--'));
    const paths = args.filter((arg) => !arg.startsWith('--'));
    if (paths.length > 1 || new Set(flags).size !== flags.length ||
        flags.some((flag) => !['--apply', '--relay-stopped'].includes(flag))) throw new Error();
    const apply = flags.includes('--apply');
    if (apply && !flags.includes('--relay-stopped')) throw new Error();
    const path = paths[0] || process.env.DATA_PATH;
    if (!path || !lstatSync(path).isFile()) throw new Error();
    const original = readFileSync(path);
    const data = JSON.parse(original.toString('utf8'));
    // Event IDs left the data file; older files may still carry them.
    if (data?.version !== 1 || ![data.installations, data.pairings, data.pendingDecisions].every(isRecord) ||
        (data.eventIds !== undefined && !isRecord(data.eventIds))) throw new Error();
    const referenced = new Set();
    for (const record of [...Object.values(data.pairings), ...Object.values(data.pendingDecisions)]) {
      if (!isRecord(record) || typeof record.installationId !== 'string' || !record.installationId) throw new Error();
      referenced.add(record.installationId);
    }
    const cutoff = Date.now() - retentionMs;
    const eligible = [];
    for (const [id, installation] of Object.entries(data.installations)) {
      if (!isRecord(installation)) throw new Error();
      if (installation.gateways !== undefined && !isRecord(installation.gateways)) throw new Error();
      if (!Object.values(installation.gateways ?? {}).every(isRecord)) throw new Error();
      const updatedAt = typeof installation.updatedAt === 'string' ? Date.parse(installation.updatedAt) : NaN;
      if (installation.active === false && Number.isFinite(updatedAt) && updatedAt <= cutoff &&
          Object.keys(installation.gateways ?? {}).length === 0 &&
          !Object.hasOwn(installation, 'gatewaySecretHash') && !referenced.has(id)) eligible.push(id);
    }
    const total = Object.keys(data.installations).length;
    if (apply && eligible.length) {
      // Offline maintenance only: the acknowledgement cannot replace stopping
      // every writer. Byte comparison detects changes before the replacement.
      const suffix = randomUUID();
      writeExclusive(`${path}.backup-${suffix}`, original);
      for (const id of eligible) delete data.installations[id];
      const tempPath = `${path}.prune-${suffix}.tmp`;
      writeExclusive(tempPath, `${JSON.stringify(data)}\n`);
      temporary = tempPath;
      if (!lstatSync(path).isFile() || !readFileSync(path).equals(original)) throw new Error();
      renameSync(temporary, path);
      temporary = undefined;
    }
    const removed = apply ? eligible.length : 0;
    process.stdout.write(`${JSON.stringify({ eligible: eligible.length, removed, remaining: total - removed })}\n`);
  } catch {
    if (temporary) {
      try { unlinkSync(temporary); } catch { /* Never disclose stored data. */ }
    }
    process.stderr.write('storage_prune_unavailable\n');
    process.exitCode = 1;
  }
}

main();
