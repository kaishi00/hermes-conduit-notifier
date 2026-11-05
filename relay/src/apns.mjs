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
// ping retires it. A ping tick that finds no send for IDLE_CLOSE_MS closes
// it, and the next send connects again. The constructor takes other values
// for tests.
const PING_INTERVAL_MS = 60_000;
const PING_TIMEOUT_MS = 10_000;
const IDLE_CLOSE_MS = 15 * 60_000;

export class ApnsClient {
  constructor({ keyPath, keyId, teamId, topic, origin, pingIntervalMs = PING_INTERVAL_MS, pingTimeoutMs = PING_TIMEOUT_MS, idleCloseMs = IDLE_CLOSE_MS }) {
    this.keyId = keyId;
    this.teamId = teamId;
    this.topic = topic;
    this.origin = origin || DEFAULT_APNS_ORIGIN;
    this.privateKey = createPrivateKey(readFileSync(keyPath));
    this.cachedToken = null;
    this.tokenCreatedAt = 0;
    this.pingIntervalMs = pingIntervalMs;
    this.pingTimeoutMs = pingTimeoutMs;
    this.idleCloseMs = idleCloseMs;
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
    // The sends in flight on this session, each by its fail(): one session
    // 'error' listener settles them all, however many share the session.
    session.pending = new Set();
    session.inflight = 0;
    session.lastSendAt = Date.now();
    this.session = session;
    const retire = () => {
      clearInterval(ping);
      if (this.session === session) this.session = null;
    };
    // Required: without an 'error' listener a connection-level failure
    // (refused, TLS, socket reset) reaches Node as an unhandled 'error'
    // event and terminates the whole relay. It also settles every send in
    // flight on the session, so a burst never stacks a listener per send.
    session.on('error', (error) => {
      retire();
      for (const fail of [...session.pending]) fail(error);
    });
    session.on('close', retire);
    // GOAWAY: APNs is closing this connection. Streams already on it may
    // still finish; new pushes go on a new one.
    session.on('goaway', () => {
      retire();
      try { session.close(); } catch { /* already closing */ }
    });
    const ping = setInterval(() => {
      if (session.closed || session.destroyed) return;
      // The pings themselves keep the socket busy, so idleness is counted
      // from the last send, not from socket activity.
      if (session.inflight === 0 && Date.now() - session.lastSendAt >= this.idleCloseMs) {
        retire();
        session.close();
        return;
      }
      const deadline = setTimeout(() => {
        retire();
        session.destroy();
      }, this.pingTimeoutMs);
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
    }, this.pingIntervalMs);
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
        session.pending.delete(fail);
        session.inflight -= 1;
        if (session.inflight === 0 && !session.destroyed) session.unref();
      };
      const fail = (error) => {
        if (settled) return;
        settled = true;
        // APNs answered before this broke (the session may fail before the
        // stream does): it may have delivered the push, so send() does not
        // send it again.
        if (status !== 0 && error && typeof error === 'object') error.apnsAnswered = true;
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
      let status = 0;
      session.inflight += 1;
      session.lastSendAt = Date.now();
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
      session.pending.add(fail);
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
      request.on('error', fail);
      // Final net so the promise settles even if the stream closes without
      // 'end' or 'error' (settle-once turns later events into no-ops).
      request.on('close', () => {
        fail(new Error('apns stream closed before settling'));
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
