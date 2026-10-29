import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

// Crash and freeze reports Conduit's testers choose to send (hermes-conduit
// MetricKitReports, the "Send report" card after a crash or a hang). iOS
// hands Conduit a MetricKit diagnostic on the next launch: call stacks,
// exception and termination codes, the build and the device model, never
// what was typed or said. The app asks once per report; Send posts it here.
//
// Anyone can post (a tester needn't use notifications), so each address and
// the relay as a whole have small budgets, and a post is capped in size.
// Reports are files of their own beside the relay's data, kept for
// MAX_AGE_DAYS and at most MAX_REPORTS; only the developer reads them, with
// CRASH_REPORTS_TOKEN. A relay without that token takes none and doesn't
// list the capability; the app stops asking when a post finds no route.

export const CRASH_REPORTS_CAPABILITY = 'crash-reports';
export const CRASH_REPORTS_ROUTE = /^\/v1\/crash-reports(?:\/([0-9a-z-]{1,64}))?$/;
// A MetricKit diagnostic payload with a few deep stacks; the app trims a
// bigger one to its crashes and hangs first.
export const MAX_REPORT_CHARS = 600_000;
export const MAX_REPORTS = 1_000;
export const MAX_AGE_DAYS = 90;
export const REPORTS_PER_ADDRESS_PER_HOUR = 10;
export const REPORTS_PER_DAY = 500;
export const KINDS = new Set(['crash', 'hang']);

const ID_PATTERN = /^\d{13}-[0-9a-f]{12}$/;
const FIELD_PATTERN = /^[\p{L}\p{N} ()._,:+-]{1,64}$/u;

export class CrashReports {
  constructor({ directory, maxReports = MAX_REPORTS, maxAgeMs = MAX_AGE_DAYS * 86_400_000, now = Date.now }) {
    this.directory = directory;
    this.maxReports = maxReports;
    this.maxAgeMs = maxAgeMs;
    this.now = now;
    mkdirSync(directory, { recursive: true, mode: 0o700 });
  }

  // Keeps one report; returns its id. The id leads with the time, so names
  // sort oldest first.
  save(report) {
    const id = `${this.now()}-${randomBytes(6).toString('hex')}`;
    writeFileSync(join(this.directory, `${id}.json`), JSON.stringify({ id, received_at: new Date(this.now()).toISOString(), ...report }), { mode: 0o600 });
    this.prune();
    return id;
  }

  ids() {
    let names;
    try {
      names = readdirSync(this.directory);
    } catch {
      return [];
    }
    return names.filter((name) => name.endsWith('.json')).map((name) => name.slice(0, -5)).filter((id) => ID_PATTERN.test(id)).sort();
  }

  // Every report's heading, oldest first, optionally of one build.
  list({ build } = {}) {
    return this.ids().map((id) => this.get(id)).filter(Boolean)
      .filter((report) => !build || report.build === build)
      .map(({ id, received_at, app_version, build: reportBuild, os, device, kind, signature }) => ({ id, received_at, app_version, build: reportBuild, os, device, kind, signature }));
  }

  get(id) {
    if (!ID_PATTERN.test(id)) return null;
    try {
      return JSON.parse(readFileSync(join(this.directory, `${id}.json`), 'utf8'));
    } catch {
      return null;
    }
  }

  prune() {
    const ids = this.ids();
    const cutoff = this.now() - this.maxAgeMs;
    ids.forEach((id, index) => {
      const old = Number(id.split('-')[0]) < cutoff;
      if (old || index < ids.length - this.maxReports) {
        try { unlinkSync(join(this.directory, `${id}.json`)); } catch { /* gone already */ }
      }
    });
  }
}

// The fields a post must carry, cleaned; throws a 400 code otherwise.
export function validateReport(body) {
  const field = (name) => {
    const value = body?.[name];
    if (typeof value !== 'string' || !FIELD_PATTERN.test(value)) throw badRequest(`invalid_${name}`);
    return value;
  };
  const report = {
    app_version: field('app_version'),
    build: field('build'),
    os: field('os'),
    device: field('device'),
    kind: field('kind'),
  };
  if (!KINDS.has(report.kind)) throw badRequest('invalid_kind');
  const diagnostic = body?.diagnostic;
  if (!diagnostic || typeof diagnostic !== 'object' || Array.isArray(diagnostic)) throw badRequest('invalid_diagnostic');
  report.diagnostic = diagnostic;
  report.signature = signature(diagnostic);
  return report;
}

// What groups reports of the same fault: the first crash's or hang's
// exception and termination codes and the first frames of its stack, hashed.
export function signature(diagnostic) {
  const first = diagnostic.crashDiagnostics?.[0] ?? diagnostic.hangDiagnostics?.[0];
  if (!first || typeof first !== 'object') return null;
  const meta = first.diagnosticMetaData ?? {};
  const frames = [];
  const walk = (frame) => {
    if (!frame || typeof frame !== 'object' || frames.length >= 8) return;
    frames.push(`${frame.binaryName ?? '?'}+${frame.offsetIntoBinaryTextSegment ?? '?'}`);
    for (const child of frame.subFrames ?? []) walk(child);
  };
  const threads = first.callStackTree?.callStacks ?? [];
  const crashed = threads.find((thread) => thread?.threadAttributed) ?? threads[0];
  for (const root of crashed?.callStackRootFrames ?? []) walk(root);
  const parts = [meta.exceptionType, meta.exceptionCode, meta.signal, meta.terminationReason, ...frames].map((part) => String(part ?? ''));
  return createHash('sha256').update(parts.join('|')).digest('hex').slice(0, 16);
}

// The routes: POST from the app, GET for the developer. Returns false for
// a request that isn't one of them.
export function crashReportRoutes({ reports, adminToken, readJson, enforceRateLimit, clientAddress }) {
  const expected = adminToken ? createHash('sha256').update(adminToken).digest() : null;
  const isAdmin = (request) => {
    const match = String(request.headers.authorization ?? '').match(/^Bearer\s+(\S+)$/);
    if (!match || !expected) return false;
    return timingSafeEqual(createHash('sha256').update(match[1]).digest(), expected);
  };
  return async function route(request, response, url, sendJson) {
    const match = url.pathname.match(CRASH_REPORTS_ROUTE);
    if (!match) return false;
    if (!reports) {
      sendJson(response, 404, { error: 'not_found' });
      return true;
    }
    const id = match[1];
    if (request.method === 'POST' && !id) {
      enforceRateLimit(`crash-report:${clientAddress(request)}`, REPORTS_PER_ADDRESS_PER_HOUR, 60 * 60_000);
      enforceRateLimit('crash-report-global', REPORTS_PER_DAY, 24 * 60 * 60_000);
      const body = await readJson(request, MAX_REPORT_CHARS);
      const saved = reports.save(validateReport(body));
      sendJson(response, 201, { id: saved });
      return true;
    }
    if (request.method === 'GET') {
      if (!isAdmin(request)) {
        enforceRateLimit(`crash-report-read:${clientAddress(request)}`, 10, 60_000);
        sendJson(response, 401, { error: 'unauthorized' });
        return true;
      }
      if (!id) {
        const build = url.searchParams.get('build') ?? undefined;
        sendJson(response, 200, { reports: reports.list({ build }) });
        return true;
      }
      const report = reports.get(id);
      sendJson(response, report ? 200 : 404, report ?? { error: 'not_found' });
      return true;
    }
    sendJson(response, 405, { error: 'method_not_allowed' });
    return true;
  };
}

// CrashReports in a folder beside the relay's data, or null when this relay
// has no CRASH_REPORTS_TOKEN (nobody could read them).
export function crashReportsFromEnv(env, dataPath) {
  const token = env.CRASH_REPORTS_TOKEN;
  if (!token) return { reports: null, adminToken: null };
  if (token.length < 32) throw new Error('CRASH_REPORTS_TOKEN must be at least 32 characters.');
  const directory = join(dirname(dataPath), 'crash-reports');
  return { reports: new CrashReports({ directory }), adminToken: token };
}

function badRequest(code) {
  const error = new Error(code);
  error.status = 400;
  return error;
}
