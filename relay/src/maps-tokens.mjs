import { createPrivateKey, createSign } from 'node:crypto';

// Apple Maps for Hermes hosts (conduit_maps' host engine). The relay holds
// Conduit's Maps key and trades a signed JWT for a short-lived Maps Server
// API access token; paired hosts fetch that token here and call Apple Maps
// themselves, so the relay never sees a lookup. One cached token serves
// every host until shortly before it expires, so a token says nothing about
// which host used it.

export const MAPS_TOKENS_CAPABILITY = 'maps-tokens';
export const MAPS_TOKEN_ROUTE = '/v1/maps/token';
const APPLE_TOKEN_URL = 'https://maps-api.apple.com/v1/token';
const AUTH_LIFETIME_S = 30 * 60;
// Hand out a token only while it has at least this long left.
const REFRESH_MARGIN_S = 120;

export class MapsTokens {
  constructor({ privateKey, keyId, teamId, fetchImpl = globalThis.fetch, now = () => Date.now() }) {
    this.privateKey = typeof privateKey === 'string' ? createPrivateKey(privateKey) : privateKey;
    this.keyId = keyId;
    this.teamId = teamId;
    this.fetch = fetchImpl;
    this.now = now;
    this.cached = null;
    this.pending = null;
  }

  // The JWT Apple's /v1/token takes (ES256, scope server_api).
  authorizationToken() {
    const issuedAt = Math.floor(this.now() / 1000);
    const header = base64url(JSON.stringify({ alg: 'ES256', kid: this.keyId, typ: 'JWT' }));
    const claims = base64url(JSON.stringify({ iss: this.teamId, iat: issuedAt, exp: issuedAt + AUTH_LIFETIME_S, scope: 'server_api' }));
    const signingInput = `${header}.${claims}`;
    const signer = createSign('SHA256');
    signer.update(signingInput);
    signer.end();
    const signature = signer.sign({ key: this.privateKey, dsaEncoding: 'ieee-p1363' }).toString('base64url');
    return `${signingInput}.${signature}`;
  }

  // { token, expiresIn } with at least REFRESH_MARGIN_S left; concurrent
  // callers share one exchange.
  async accessToken() {
    const nowS = Math.floor(this.now() / 1000);
    if (this.cached && this.cached.expiresAt - nowS > REFRESH_MARGIN_S) {
      return { token: this.cached.token, expiresIn: this.cached.expiresAt - nowS };
    }
    if (!this.pending) {
      this.pending = this.exchange().finally(() => { this.pending = null; });
    }
    const fresh = await this.pending;
    return { token: fresh.token, expiresIn: fresh.expiresAt - Math.floor(this.now() / 1000) };
  }

  async exchange() {
    const response = await this.fetch(APPLE_TOKEN_URL, {
      method: 'GET',
      headers: { Authorization: `Bearer ${this.authorizationToken()}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      const error = new Error(`apple_maps_token_${response.status}`);
      error.status = response.status === 429 ? 429 : 502;
      throw error;
    }
    const body = await response.json();
    const token = typeof body?.accessToken === 'string' ? body.accessToken : '';
    const lifetime = Number(body?.expiresInSeconds);
    if (!token || !Number.isFinite(lifetime) || lifetime <= REFRESH_MARGIN_S) {
      const error = new Error('apple_maps_token_malformed');
      error.status = 502;
      throw error;
    }
    this.cached = { token, expiresAt: Math.floor(this.now() / 1000) + Math.floor(lifetime) };
    return this.cached;
  }
}

// MapsTokens from the environment, or null when this relay has no Maps key
// (MAPS_KEY_PATH + MAPS_KEY_ID; the team is APNS_TEAM_ID unless
// MAPS_TEAM_ID says otherwise). MAPS_MODE=fake is a test seam that hands out
// a fixed token without calling Apple.
export function mapsTokensFromEnv(env, { readFile, warn }) {
  if (env.MAPS_MODE === 'fake') {
    warn('MAPS_MODE=fake: hosts get a test token that Apple refuses; unset it for real maps');
    return { accessToken: async () => ({ token: 'fake-maps-token', expiresIn: 1800 }) };
  }
  if (!env.MAPS_KEY_PATH || !env.MAPS_KEY_ID) return null;
  try {
    return new MapsTokens({ privateKey: readFile(env.MAPS_KEY_PATH, 'utf8'), keyId: env.MAPS_KEY_ID, teamId: env.MAPS_TEAM_ID || env.APNS_TEAM_ID });
  } catch (error) {
    // A missing or unreadable key turns maps off, not the relay.
    warn('maps key unreadable; maps tokens are off', { error: error instanceof Error ? error.message : String(error) });
    return null;
  }
}

function base64url(value) {
  return Buffer.from(value).toString('base64url');
}
