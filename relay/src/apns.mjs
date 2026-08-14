import { createPrivateKey, createSign } from 'node:crypto';
import { connect } from 'node:http2';
import { readFileSync } from 'node:fs';

export class ApnsClient {
  constructor({ keyPath, keyId, teamId, topic }) {
    this.keyId = keyId;
    this.teamId = teamId;
    this.topic = topic;
    this.privateKey = createPrivateKey(readFileSync(keyPath));
    this.cachedToken = null;
    this.tokenCreatedAt = 0;
  }

  async send(deviceToken, notification) {
    const client = connect('https://api.push.apple.com');
    try {
      return await new Promise((resolve, reject) => {
        const request = client.request({
          ':method': 'POST',
          ':path': `/3/device/${deviceToken}`,
          authorization: `bearer ${this.authorizationToken()}`,
          'apns-topic': this.topic,
          'apns-push-type': 'alert',
          'apns-priority': '10',
          ...(notification.collapseId ? { 'apns-collapse-id': notification.collapseId.slice(0, 64) } : {}),
        });
        let status = 0;
        let body = '';
        request.setEncoding('utf8');
        request.on('response', (headers) => { status = Number(headers[':status'] ?? 0); });
        request.on('data', (chunk) => { body += chunk; });
        request.on('end', () => {
          let detail = null;
          try { detail = body ? JSON.parse(body) : null; } catch { detail = null; }
          resolve({ ok: status === 200, status, reason: detail?.reason ?? null });
        });
        request.on('error', reject);
        request.end(JSON.stringify(notification.payload));
      });
    } finally {
      client.close();
    }
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
