import { readFileSync } from 'node:fs';
import { MAX_INSTALLATIONS } from './store.mjs';

function main() {
  const dataPath = process.argv[2] || process.env.DATA_PATH;
  if (!dataPath || process.argv.length > 3) return fail();

  try {
    const data = JSON.parse(readFileSync(dataPath, 'utf8'));
    if (data?.version !== 1 || !data.installations || typeof data.installations !== 'object' || Array.isArray(data.installations)) {
      return fail();
    }
    const records = Object.values(data.installations);
    const active = records.filter((installation) => installation?.active === true).length;
    process.stdout.write(`${JSON.stringify({
      installations: { total: records.length, active, inactive: records.length - active, capacity: MAX_INSTALLATIONS },
    })}\n`);
  } catch {
    return fail();
  }
}

function fail() {
  process.stderr.write('storage_status_unavailable\n');
  process.exitCode = 1;
}

main();
