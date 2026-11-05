import { createPrivateKey, createSign } from 'node:crypto';
import { connect } from 'node:http2';
import { readFileSync } from 'node:fs';

// Production origin; the constructor override exists so tests can point the
// real transport at a failing endpoint (closed port, protocol-breaking peer)
// without mocking the client away.
const DEFAULT_APNS_ORIGIN = 'https://api.push.apple.com';
// Bound for one APNs send. APNs answers in well under a second in practice;
// this exists for the one failure shape that emits NO event to settle on —
// a peer that accepts the connection and then stalls — so the send promise
// always settles and the intake request can never hang on it.
const SEND_TIMEOUT_MS = 10_000;
// One HTTP/2 session is kept open and shared by every send (#560), as Apple
// asks: a fresh TCP + TLS handshake per push cost one or two round trips on
// every reply, card and ring. While it is open a ping checks it every
// minute, so a connection that died without a word (a NAT that dropped it)
// is noticed before a push waits out SEND_TIMEOUT_MS on it; an unanswered
// ping retires it. After IDLE_CLOSE_MS without a send it is closed, and the
// next send connects again.
const PING_INTERVAL_MS = 60_000;
const PING_TIMEOUT_MS = 10_000;
const IDLE_CLOSE_MS = 15 * 60_000;

export class ApnsClient {
  constructor({ keyPath, keyId, teamId, topic, origin }) {
    this.keyId = keyId;
    this.teamId = teamId;
    this.topic = topic;
    this.origin = origin || DEFAULT_APNS_ORIGIN;
    this.privateKey = createPrivateKey(readFileSync(keyPath));
    this.cachedToken = null;
    this.tokenCreatedAt = 0;
    this.session = null;
    // Sessions opened, for tests and the logs.
    this.connects = 0;
  }

  async send(deviceToken, notification) {
    const reused = Boolean(this.session && !this.session.closed && !this.session.destroyed);
    const session = this.currentSession();
    try {
      return await this.sendOn(session, deviceToken, notification);
    } catch (error) {
      // A kept session can be gone by the time a push uses it (APNs closed
      // it, or it broke between pings). When it failed before APNs answered
      // and before the send timed out, send once more on a fresh one rather
      // than failing the push. A fresh session's failure is the answer.
      if (!reused || error?.apnsTimedOut || error?.apnsAnswered) throw error;
      return await this.sendOn(this.currentSession(), deviceToken, notification);
    }
  }

  // The open session, or a new one. Every way a session ends (error,
  // GOAWAY, close, a missed ping, idle) retires it, so the next send
  // connects again instead of failing on a dead one.
  currentSession() {
    if (this.session && !this.session.closed && !this.session.destroyed) return this.session;
    const session = connect(this.origin);
    this.connects += 1;
    session.inflight = 0;
    this.session = session;
    const retire = () => {
      clearInterval(ping);
      if (this.session === session) this.session = null;
    };
    // Required: without an 'error' listener a connection-level failure
    // (refused, TLS, socket reset) reaches Node as an unhandled 'error'
    // event and terminates the whole relay. The streams in flight on it fail
    // through their own handlers in sendOn.
    session.on('error', retire);
    session.on('close', retire);
    // GOAWAY: APNs is closing this connection. Streams already on it may
    // still finish; new pushes go on a new one.
    session.on('goaway', () => {
      retire();
      try { session.close(); } catch { /* already closing */ }
    });
    session.setTimeout(IDLE_CLOSE_MS, () => {
      retire();
      session.close();
    });
    const ping = setInterval(() => {
      if (session.closed || session.destroyed) return;
      const deadline = setTimeout(() => {
        retire();
        session.destroy();
      }, PING_TIMEOUT_MS);
      deadline.unref?.();
      try {
        session.ping((error) => {
          clearTimeout(deadline);
          if (error) { retire(); session.destroy(); }
        });
      } catch {
        clearTimeout(deadline);
        retire();
        session.destroy();
      }
    }, PING_INTERVAL_MS);
    ping.unref?.();
    // An idle session never holds the process open; sendOn refs it while a
    // push is in flight.
    session.unref();
    return session;
  }

  // Ends the kept session: on shutdown, and in tests.
  close() {
    const session = this.session;
    this.session = null;
    if (session && !session.destroyed) session.close();
  }

  sendOn(session, deviceToken, notification) {
    return new Promise((resolve, reject) => {
      // A failing APNs connection can surface through BOTH the request stream
      // and the session, and a late session error can arrive after the
      // response already completed. settle-once makes every path settle the
      // SAME promise exactly once. The session's own 'error' listener
      // (currentSession) is what keeps a connection-level failure from
      // crashing the relay; this one only settles this send.
      let settled = false;
      let request;
      const finish = () => {
        clearTimeout(stall);
        session.off('error', fail);
        session.inflight -= 1;
        if (session.inflight === 0 && !session.destroyed) session.unref();
      };
      const fail = (error) => {
        if (settled) return;
        settled = true;
        finish();
        // Cancel only this stream: the session is shared with other sends,
        // and its own listeners retire it if it is the thing that broke.
        try { if (request && !request.destroyed) request.close(); } catch { /* already gone */ }
        reject(error);
      };
      const succeed = (result) => {
        if (settled) return;
        settled = true;
        finish();
        resolve(result);
      };
      session.inflight += 1;
      session.ref();
      const stall = setTimeout(() => {
        // A session that leaves a push unanswered this long is not one to
        // keep: the next send connects again.
        if (this.session === session) this.session = null;
        try { session.close(); } catch { /* already closing */ }
        const error = new Error('apns send timed out');
        error.apnsTimedOut = true;
        fail(error);
      }, SEND_TIMEOUT_MS);
      session.on('error', fail);
      try {
        request = session.request({
          ':method': 'POST',
          ':path': `/3/device/${deviceToken}`,
          authorization: `bearer ${this.authorizationToken()}`,
          // A call (#449) rings through PushKit: its own push type, the
          // app's `.voip` topic and an expiration.
          'apns-topic': notification.topic || this.topic,
          'apns-push-type': notification.pushType || 'alert',
          // A background push (a calendar change applied without asking)
          // must go at priority 5.
          'apns-priority': String(notification.priority ?? 10),
          ...(notification.expiration !== undefined ? { 'apns-expiration': String(notification.expiration) } : {}),
          ...(notification.collapseId ? { 'apns-collapse-id': notification.collapseId.slice(0, 64) } : {}),
        });
      } catch (error) {
        fail(error);
        return;
      }
      let status = 0;
      let body = '';
      request.setEncoding('utf8');
      request.on('response', (headers) => { status = Number(headers[':status'] ?? 0); });
      request.on('data', (chunk) => { body += chunk; });
      request.on('end', () => {
        // A legitimate APNs answer always carries :status; a stream that
        // ended without one is a transport break (abrupt close, GOAWAY
        // teardown) — fail it rather than "resolving" a status-0 non-answer
        // the server would misread as an APNs rejection.
        if (status === 0) {
          fail(new Error('apns connection closed before a response arrived'));
          return;
        }
        let detail = null;
        try { detail = body ? JSON.parse(body) : null; } catch { detail = null; }
        succeed({ ok: status === 200, status, reason: detail?.reason ?? null });
      });
      request.on('error', (error) => {
        // Answered, then broke: APNs may have delivered it, so it is not
        // sent again.
        if (status !== 0 && error && typeof error === 'object') error.apnsAnswered = true;
        fail(error);
      });
      // Final net so the promise settles even if the stream closes without
      // 'end' or 'error' (settle-once turns later events into no-ops).
      request.on('close', () => {
        const error = new Error('apns stream closed before settling');
        if (status !== 0) error.apnsAnswered = true;
        fail(error);
      });
      try {
        request.end(JSON.stringify(notification.payload));
      } catch (error) {
        fail(error);
      }
    });
  }

  authorizationToken() {
    const now = Math.floor(Date.now() / 1000);
    if (this.cachedToken && now - this.tokenCreatedAt < 50 * 60) return this.cachedToken;
    const header = base64url(JSON.stringify({ alg: 'ES256', kid: this.keyId }));
    const claims = base64url(JSON.stringify({ iss: this.teamId, iat: now }));
    const signingInput = `${header}.${claims}`;
    const signer = createSign('SHA256');
    signer.update(signingInput);
    signer.end();
    const signature = signer.sign({ key: this.privateKey, dsaEncoding: 'ieee-p1363' }).toString('base64url');
    this.cachedToken = `${signingInput}.${signature}`;
    this.tokenCreatedAt = now;
    return this.cachedToken;
  }
}

function base64url(value) {
  return Buffer.from(value).toString('base64url');
}
