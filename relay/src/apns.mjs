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

export class ApnsClient {
  constructor({ keyPath, keyId, teamId, topic, origin }) {
    this.keyId = keyId;
    this.teamId = teamId;
    this.topic = topic;
    this.origin = origin || DEFAULT_APNS_ORIGIN;
    this.privateKey = createPrivateKey(readFileSync(keyPath));
    this.cachedToken = null;
    this.tokenCreatedAt = 0;
  }

  async send(deviceToken, notification) {
    const client = connect(this.origin);
    return await new Promise((resolve, reject) => {
      // A failing APNs connection can surface through BOTH the request stream
      // and the ClientHttp2Session, and a late session error can arrive after
      // the response already completed. settle-once makes every path settle
      // the SAME promise exactly once — critically, the session 'error'
      // handler below is what keeps a connection-level failure (refused,
      // TLS, GOAWAY, socket reset) from reaching Node as an unhandled
      // 'error' event, which would terminate the whole relay process.
      let settled = false;
      const fail = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(stall);
        // A failed session cannot be closed gracefully; destroy instead of
        // close so cleanup itself cannot hang or throw (destroy is safe on
        // an already-dead session).
        client.destroy();
        reject(error);
      };
      const succeed = (result) => {
        if (settled) return;
        settled = true;
        clearTimeout(stall);
        client.close();
        resolve(result);
      };
      const stall = setTimeout(() => fail(new Error('apns send timed out')), SEND_TIMEOUT_MS);
      client.on('error', fail);
      let request;
      try {
        request = client.request({
          ':method': 'POST',
          ':path': `/3/device/${deviceToken}`,
          authorization: `bearer ${this.authorizationToken()}`,
          'apns-topic': this.topic,
          'apns-push-type': 'alert',
          'apns-priority': '10',
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
    // Each send owns one short-lived session that is closed on success or
    // destroyed on failure before the promise settles, so its listeners are
    // released with the session — nothing is attached beyond this call.
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
