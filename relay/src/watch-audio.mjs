import { createHash } from 'node:crypto';

// Live audio for a Conduit Watch call (hermes-conduit
// designs/apple-watch-gpt-live.md, "Bridge design").
//
// GPT-Live and Grok both need this host's sign-in, and GPT-Live also needs
// WebRTC, which the Watch can't run. So for those engines the host holds the
// provider session and the Watch streams to it through here: the host dials
// /v1/watch-audio/{grant}/host when it opens the grant, the Watch dials
// /v1/watch-audio/{grant}/watch, and this relay copies binary WebSocket
// messages between the two. Like the tool grants, it is a rendezvous only:
// every message is sealed on the Watch and the host with a per-stream key
// this relay never sees, so it can neither read nor forge one.
//
// The one exception is a relay notice, a two-byte message the relay itself
// sends to the host: [0x00, NOTICE_*]. That makes the first byte part of the
// watch-audio-v1 contract: every message a host or Watch sends is non-empty
// and starts with its message type, 1-255 (the plugin's and Conduit's sealed
// frames, hello included). A message that is empty or starts with 0x00 is a
// protocol error and closes the sender's socket with 1002.
//
// The WebSocket server is the small subset of RFC 6455 both clients use
// (URLSessionWebSocketTask on the Watch, the `websockets` package on the
// host): unfragmented binary messages, ping, pong and close. The relay has no
// npm dependencies, and this keeps it that way.

export const WATCH_AUDIO_CAPABILITY = 'watch-audio-v1';

// A 20 ms PCM frame is 640-960 bytes sealed; an engine event (a transcript,
// a session update with the call's briefing) can be several KB.
export const MAX_MESSAGE_BYTES = 64 * 1024;
// Averaged over RATE_WINDOW_MS, per direction. 24 kHz PCM down is 48 KB/s
// before framing; this leaves room for events and bursts.
export const MAX_BYTES_PER_S = 96 * 1024;
export const RATE_WINDOW_MS = 5_000;
// Both ends ping well inside this; a silent socket is a dead one.
export const IDLE_MS = 60_000;
// What every frame costs against the rate limit on top of its size, so a
// stream of tiny frames (pings, say) can't outrun it. 50 audio frames a
// second spend 3.2 KB/s of it.
export const FRAME_COST_BYTES = 64;

export const NOTICE_WATCH_CONNECTED = 1;
export const NOTICE_WATCH_GONE = 2;

// Close codes (4000-4999 are the application's).
export const CLOSE_REPLACED = 4000;
export const CLOSE_GRANT_CLOSED = 4010;
export const CLOSE_IDLE = 4408;
export const CLOSE_TOO_FAST = 4429;
export const CLOSE_HOST_OFFLINE = 4503;

const ROUTE = /^\/v1\/watch-audio\/([A-Za-z0-9_-]{22})\/(watch|host)$/;
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const KEY_PATTERN = /^[A-Za-z0-9+/]{22}==$/;

export class WatchAudioBridges {
  constructor({ grants, maxBridges, maxPerGateway }) {
    this.grants = grants;
    this.maxBridges = maxBridges;
    this.maxPerGateway = maxPerGateway;
    // grant id -> { grant, host, watch }
    this.bridges = new Map();
  }

  get size() {
    return this.bridges.size;
  }

  // Closes bridges whose grant expired or was closed.
  sweep() {
    for (const [id, bridge] of [...this.bridges]) {
      if (!this.grants.live(id) || this.grants.grants.get(id) !== bridge.grant) {
        this.end(bridge, CLOSE_GRANT_CLOSED, 'grant_closed');
      }
    }
  }

  end(bridge, code, reason) {
    if (this.bridges.get(bridge.grant.id) === bridge) this.bridges.delete(bridge.grant.id);
    // Both sides are cleared first, so neither close sends the other a notice.
    const { watch, host } = bridge;
    bridge.watch = null;
    bridge.host = null;
    watch?.close(code, reason);
    host?.close(code, reason);
  }

  // Every bridge, for a relay that is shutting down.
  endAll(code, reason) {
    for (const bridge of [...this.bridges.values()]) this.end(bridge, code, reason);
  }

  // Whether a host socket for `grant` would be taken: a bridge that exists
  // always takes its host back; a new one needs room.
  canAttachHost(grant) {
    if (this.bridges.has(grant.id)) return true;
    let owned = 0;
    for (const other of this.bridges.values()) {
      if (other.grant.installationId === grant.installationId && other.grant.gatewayId === grant.gatewayId) owned += 1;
    }
    return owned < this.maxPerGateway && this.bridges.size < this.maxBridges;
  }

  // A host socket for `grant` (already authenticated as its gateway).
  attachHost(grant, socket) {
    let bridge = this.bridges.get(grant.id);
    if (!bridge) {
      if (!this.canAttachHost(grant)) return false;
      bridge = { grant, host: null, watch: null };
      this.bridges.set(grant.id, bridge);
    }
    if (bridge.host) {
      // A host that reconnects starts over: its provider session went with
      // the old socket, so the Watch must rejoin too.
      const old = bridge.host;
      bridge.host = null;
      old.close(CLOSE_REPLACED, 'replaced');
      if (bridge.watch) {
        const watch = bridge.watch;
        bridge.watch = null;
        watch.close(CLOSE_HOST_OFFLINE, 'host_reconnected');
      }
    }
    bridge.host = socket;
    socket.onMessage = (payload) => this.forward(bridge, socket, bridge.watch, payload);
    socket.onClosed = () => {
      if (bridge.host !== socket) return;
      bridge.host = null;
      // The host's provider session is gone with it.
      if (bridge.watch) {
        const watch = bridge.watch;
        bridge.watch = null;
        watch.close(CLOSE_HOST_OFFLINE, 'host_offline');
      }
      if (this.bridges.get(grant.id) === bridge) this.bridges.delete(grant.id);
    };
    return true;
  }

  // A Watch socket for `grant` (already authenticated with its relay key).
  // Refused unless the host is there: nothing is ever queued for it.
  attachWatch(grant, socket) {
    const bridge = this.bridges.get(grant.id);
    if (!bridge?.host) {
      socket.close(CLOSE_HOST_OFFLINE, 'host_offline');
      return;
    }
    if (bridge.watch) {
      const old = bridge.watch;
      bridge.watch = null;
      old.close(CLOSE_REPLACED, 'replaced');
      bridge.host.send(Buffer.from([0, NOTICE_WATCH_GONE]));
    }
    bridge.watch = socket;
    bridge.host.send(Buffer.from([0, NOTICE_WATCH_CONNECTED]));
    socket.onMessage = (payload) => this.forward(bridge, socket, bridge.host, payload);
    socket.onClosed = () => {
      if (bridge.watch !== socket) return;
      bridge.watch = null;
      bridge.host?.send(Buffer.from([0, NOTICE_WATCH_GONE]));
    };
  }

  forward(bridge, from, to, payload) {
    // Only the relay sends notices.
    if (payload.length === 0 || payload[0] === 0) {
      from.close(1002, 'reserved_type');
      return;
    }
    if (!to) {
      // The Watch spoke before (or after) its host: say so, never queue.
      if (from === bridge.watch) from.close(CLOSE_HOST_OFFLINE, 'host_offline');
      return;
    }
    // Backpressure: a slow reader pauses the writer instead of buffering here.
    if (!to.send(payload)) {
      from.pause();
      to.onDrain = () => from.resume();
    }
  }
}

// One server-side WebSocket over a raw upgraded socket.
export class RelaySocket {
  constructor(socket, { idleMs = IDLE_MS, maxBytesPerS = MAX_BYTES_PER_S, now = Date.now } = {}) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.closed = false;
    this.onMessage = () => {};
    this.onClosed = () => {};
    this.onDrain = null;
    this.idleMs = idleMs;
    this.maxWindowBytes = Math.floor(maxBytesPerS * RATE_WINDOW_MS / 1000);
    this.now = now;
    this.windowStart = now();
    this.windowBytes = 0;
    socket.setNoDelay(true);
    socket.on('data', (chunk) => this.receive(chunk));
    socket.on('drain', () => {
      const drain = this.onDrain;
      this.onDrain = null;
      drain?.();
    });
    socket.on('close', () => this.finish());
    socket.on('error', () => this.finish());
    this.touch();
  }

  touch() {
    clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => this.close(CLOSE_IDLE, 'idle'), this.idleMs);
    this.idleTimer.unref?.();
  }

  pause() {
    this.socket.pause();
  }

  resume() {
    this.socket.resume();
  }

  receive(chunk) {
    if (this.closed) return;
    this.buffer = this.buffer.length ? Buffer.concat([this.buffer, chunk]) : chunk;
    while (!this.closed) {
      const frame = parseFrame(this.buffer);
      if (frame === undefined) return;
      if (frame.error) {
        this.close(frame.error.code, frame.error.reason);
        return;
      }
      this.buffer = this.buffer.subarray(frame.length);
      this.touch();
      // Every frame counts, control frames included.
      if (!this.meter(frame.length + FRAME_COST_BYTES)) {
        this.close(CLOSE_TOO_FAST, 'too_fast');
        return;
      }
      this.handle(frame);
    }
  }

  // False once this socket has sent more than its window allows.
  meter(bytes) {
    const now = this.now();
    if (now - this.windowStart >= RATE_WINDOW_MS) {
      this.windowStart = now;
      this.windowBytes = 0;
    }
    this.windowBytes += bytes;
    return this.windowBytes <= this.maxWindowBytes;
  }

  handle({ opcode, payload }) {
    switch (opcode) {
      case 0x2:
        this.onMessage(payload);
        return;
      case 0x8:
        // Echo the peer's code, then hang up.
        this.close(closeReply(payload), '');
        return;
      case 0x9:
        this.write(0xA, payload);
        return;
      case 0xA:
        return;
      default:
        // Text, or a fragment: neither is part of this protocol.
        this.close(1003, 'unsupported');
    }
  }

  // False when the socket's buffer is full (wait for onDrain).
  send(payload) {
    if (this.closed) return true;
    return this.write(0x2, payload);
  }

  write(opcode, payload) {
    return this.socket.write(Buffer.concat([frameHeader(opcode, payload.length), payload]));
  }

  close(code = 1000, reason = '') {
    if (this.closed) return;
    const body = Buffer.alloc(2 + Buffer.byteLength(reason));
    body.writeUInt16BE(code, 0);
    body.write(reason, 2);
    try {
      this.write(0x8, body);
      this.socket.end();
    } catch {
      // Already gone.
    }
    this.finish();
    // A peer that never answers the close doesn't hold the socket.
    setTimeout(() => this.socket.destroy(), 1_000).unref?.();
  }

  finish() {
    if (this.closed) {
      return;
    }
    this.closed = true;
    clearTimeout(this.idleTimer);
    const drain = this.onDrain;
    this.onDrain = null;
    drain?.();
    this.onClosed();
  }
}

// Parses one client frame from `buffer`: undefined when incomplete,
// { error } for a protocol violation, else { opcode, payload, length }.
export function parseFrame(buffer, maxBytes = MAX_MESSAGE_BYTES) {
  if (buffer.length < 2) return undefined;
  const fin = (buffer[0] & 0x80) !== 0;
  const rsv = buffer[0] & 0x70;
  const opcode = buffer[0] & 0x0f;
  const masked = (buffer[1] & 0x80) !== 0;
  let length = buffer[1] & 0x7f;
  let offset = 2;
  if (rsv) return { error: { code: 1002, reason: 'rsv' } };
  // RFC 6455 5.1: a server closes on an unmasked client frame.
  if (!masked) return { error: { code: 1002, reason: 'unmasked' } };
  // A fragmented control frame breaks the protocol; a fragmented message is
  // only one this server doesn't take.
  if (!fin) return { error: opcode >= 0x8 ? { code: 1002, reason: 'fragmented_control' } : { code: 1003, reason: 'fragmented' } };
  if (length === 126) {
    if (buffer.length < 4) return undefined;
    length = buffer.readUInt16BE(2);
    offset = 4;
    // RFC 6455 5.2: the shortest length encoding only.
    if (length < 126) return { error: { code: 1002, reason: 'length_encoding' } };
  } else if (length === 127) {
    if (buffer.length < 10) return undefined;
    const high = buffer.readUInt32BE(2);
    if (high !== 0) return { error: { code: 1009, reason: 'too_big' } };
    length = buffer.readUInt32BE(6);
    offset = 10;
    if (length < 65536) return { error: { code: 1002, reason: 'length_encoding' } };
  }
  if (opcode >= 0x8 && length > 125) return { error: { code: 1002, reason: 'control_too_big' } };
  if (length > maxBytes) return { error: { code: 1009, reason: 'too_big' } };
  if (buffer.length < offset + 4 + length) return undefined;
  const mask = buffer.subarray(offset, offset + 4);
  const payload = Buffer.alloc(length);
  for (let i = 0; i < length; i += 1) payload[i] = buffer[offset + 4 + i] ^ mask[i & 3];
  return { opcode, payload, length: offset + 4 + length };
}

// The code to answer a peer's close with: theirs when it may go on the wire
// (RFC 6455 7.4 and the IANA registry), 1000 when they sent none, else 1002.
export function closeReply(payload) {
  if (payload.length === 0) return 1000;
  if (payload.length < 2) return 1002;
  const code = payload.readUInt16BE(0);
  const valid = (code >= 1000 && code <= 1003) || (code >= 1007 && code <= 1014) || (code >= 3000 && code <= 4999);
  return valid ? code : 1002;
}

export function frameHeader(opcode, length) {
  if (length < 126) return Buffer.from([0x80 | opcode, length]);
  if (length < 65536) {
    const header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(length, 2);
    return header;
  }
  const header = Buffer.alloc(10);
  header[0] = 0x80 | opcode;
  header[1] = 127;
  header.writeUInt32BE(0, 2);
  header.writeUInt32BE(length, 6);
  return header;
}

export function acceptKey(key) {
  return createHash('sha1').update(key + WS_GUID).digest('base64');
}

// The server's 'upgrade' handler for /v1/watch-audio/. Returns false for any
// other path (the caller then refuses the upgrade).
export function watchAudioUpgrade({
  bridges,
  grants,
  enforceRateLimit,
  authenticateGateway,
  clientAddress,
  socketOptions = {},
}) {
  return function upgrade(request, socket, head, url) {
    const match = url.pathname.match(ROUTE);
    if (!match) return false;
    const [, id, side] = match;
    // An error on a socket nobody listens to would take the process down.
    socket.on('error', () => socket.destroy());
    let upgraded = false;
    try {
      const key = String(request.headers['sec-websocket-key'] ?? '');
      if (request.method !== 'GET'
        || String(request.headers.upgrade ?? '').toLowerCase() !== 'websocket'
        || request.headers['sec-websocket-version'] !== '13'
        || !KEY_PATTERN.test(key)) {
        return refuse(socket, 400, 'bad_upgrade');
      }
      let grant;
      if (side === 'host') {
        // Rate-limited before the credential is checked, like the Watch's side.
        enforceRateLimit(`watch-audio-host-ip:${clientAddress(request)}`, 120, 60_000);
        const gateway = authenticateGateway(request);
        if (!gateway) return refuse(socket, 401, 'unauthorized');
        grant = grants.owned(id, gateway.installationId, gateway.gatewayId);
        if (!grant) return refuse(socket, 410, 'grant_closed');
        enforceRateLimit(`watch-audio-host:${grant.id}`, 30, 60_000);
      } else {
        // Public route: rate-limited before the key is checked.
        enforceRateLimit(`watch-audio-ip:${clientAddress(request)}`, 60, 60_000);
        grant = grants.authorizedWatch(id, bearerToken(request));
        if (!grant) return refuse(socket, 401, 'unauthorized');
        enforceRateLimit(`watch-audio-watch:${grant.id}`, 30, 60_000);
      }
      if (!grant.audio) return refuse(socket, 403, 'audio_not_granted');
      // Checked before the 101, so a full relay answers in HTTP; attachHost
      // below asks the same question synchronously, so it can't change.
      if (side === 'host' && !bridges.canAttachHost(grant)) return refuse(socket, 503, 'watch_audio_capacity');
      upgraded = true;
      socket.write([
        'HTTP/1.1 101 Switching Protocols',
        'Upgrade: websocket',
        'Connection: Upgrade',
        `Sec-WebSocket-Accept: ${acceptKey(key)}`,
        '', '',
      ].join('\r\n'));
      const relaySocket = new RelaySocket(socket, socketOptions);
      if (side === 'host') {
        if (!bridges.attachHost(grant, relaySocket)) relaySocket.close(CLOSE_HOST_OFFLINE, 'capacity');
      } else {
        bridges.attachWatch(grant, relaySocket);
      }
      if (head?.length) relaySocket.receive(head);
      return true;
    } catch (error) {
      // Past the 101 the stream is WebSocket, where an HTTP answer has no place.
      if (upgraded) {
        socket.destroy();
        return true;
      }
      const status = Number(error?.status ?? 500);
      return refuse(socket, status, status < 500 && error instanceof Error ? error.message : 'internal_error');
    }
  };
}

function refuse(socket, status, code) {
  const body = JSON.stringify({ error: code });
  const reasons = { 400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found', 410: 'Gone', 429: 'Too Many Requests', 503: 'Service Unavailable' };
  try {
    socket.end([
      `HTTP/1.1 ${status} ${reasons[status] ?? 'Error'}`,
      'Content-Type: application/json; charset=utf-8',
      `Content-Length: ${Buffer.byteLength(body)}`,
      'Cache-Control: no-store',
      'Connection: close',
      '', body,
    ].join('\r\n'));
  } catch {
    socket.destroy();
  }
  return true;
}

function bearerToken(request) {
  const match = String(request.headers.authorization ?? '').match(/^Bearer\s+(\S+)$/i);
  return match ? match[1] : undefined;
}
