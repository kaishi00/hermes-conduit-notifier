import { DatabaseSync } from 'node:sqlite';
import { inboxIdFor } from './auth.mjs';

export class MailboxError extends Error {
  constructor(code, status) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

// Bodies are opaque: sealed by the sender's bridge to the inbox owner's key.
// The mailbox keeps them with their size, times and the sender id the owner
// chose, and nothing else. Each inbox numbers its own envelopes, so the
// numbers say nothing about other inboxes' traffic.
const SCHEMA = `
CREATE TABLE IF NOT EXISTS inboxes (
  id TEXT PRIMARY KEY,
  key BLOB NOT NULL,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  last_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS senders (
  inbox TEXT NOT NULL REFERENCES inboxes (id),
  sender TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_sent_at INTEGER,
  PRIMARY KEY (inbox, sender),
  UNIQUE (inbox, token_hash)
);
CREATE TABLE IF NOT EXISTS envelopes (
  inbox TEXT NOT NULL,
  seq INTEGER NOT NULL,
  sender TEXT NOT NULL,
  idempotency_key TEXT,
  received_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  size INTEGER NOT NULL,
  body BLOB NOT NULL,
  PRIMARY KEY (inbox, seq),
  FOREIGN KEY (inbox, sender) REFERENCES senders (inbox, sender)
);
-- So the minute's prune finds idle inboxes without reading a million rows.
CREATE INDEX IF NOT EXISTS inboxes_by_last_seen ON inboxes (last_seen_at);
CREATE INDEX IF NOT EXISTS envelopes_by_sender ON envelopes (inbox, sender);
CREATE INDEX IF NOT EXISTS envelopes_by_expiry ON envelopes (expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS envelopes_by_idempotency_key
  ON envelopes (inbox, sender, idempotency_key) WHERE idempotency_key IS NOT NULL;
`;

// Kept in PRAGMA user_version, and raised whenever the tables change.
const SCHEMA_VERSION = 1;
const DAY_SECONDS = 86_400;
// What one poll returns at most, before base64; always at least one envelope.
export const MAX_PAGE_BYTES = 1_048_576;

export class MailboxStore {
  constructor(path, limits, { now = () => Math.floor(Date.now() / 1000) } = {}) {
    this.limits = limits;
    this.now = now;
    this.statements = new Map();
    this.db = new DatabaseSync(path);
    this.db.exec('PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;');
    // Kept in memory so a send doesn't sum the whole table; expired rows
    // count until the next prune. A transaction's change applies on commit.
    this.totalBytes = 0;
    this.pendingBytes = undefined;
    try {
      this.#transaction(() => this.#createSchema());
    } catch (error) {
      this.db.close();
      throw error;
    }
    this.totalBytes = Number(this.#get('SELECT COALESCE(SUM(size), 0) AS bytes FROM envelopes').bytes);
  }

  close() {
    this.db.close();
  }

  register(key) {
    const inbox = inboxIdFor(key);
    const now = this.now();
    if (this.#get('SELECT 1 AS found FROM inboxes WHERE id = ?', inbox)) {
      this.touch(inbox);
      return { inbox, created: false };
    }
    if (this.#get('SELECT COUNT(*) AS count FROM inboxes').count >= this.limits.maxInboxes) {
      throw new MailboxError('mailbox_full', 507);
    }
    // Its envelopes are numbered on from its creation time, a thousand a
    // second, so an inbox made again under the same key numbers on past the
    // earlier one unless that one averaged more.
    this.#run('INSERT INTO inboxes (id, key, created_at, last_seen_at, last_seq) VALUES (?, ?, ?, ?, ?)', inbox, key, now, now, now * 1000);
    return { inbox, created: true };
  }

  inboxKey(inbox) {
    const row = this.#get('SELECT key FROM inboxes WHERE id = ?', inbox);
    return row ? Buffer.from(row.key) : undefined;
  }

  // An owner request: keeps the inbox from being pruned as idle.
  touch(inbox) {
    this.#run('UPDATE inboxes SET last_seen_at = ? WHERE id = ?', this.now(), inbox);
  }

  inboxStatus(inbox) {
    const pending = this.#get('SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM envelopes WHERE inbox = ? AND expires_at > ?',
      inbox, this.now());
    const senders = this.#get('SELECT COUNT(*) AS count FROM senders WHERE inbox = ?', inbox);
    return { pending: Number(pending.count), pending_bytes: Number(pending.bytes), senders: Number(senders.count) };
  }

  deleteInbox(inbox) {
    return this.#transaction(() => {
      this.#release(this.#all('DELETE FROM envelopes WHERE inbox = ? RETURNING size', inbox));
      this.#run('DELETE FROM senders WHERE inbox = ?', inbox);
      return this.#run('DELETE FROM inboxes WHERE id = ?', inbox).changes > 0;
    });
  }

  // Adds a sender, or gives an existing one a new token (its envelopes stay).
  putSender(inbox, sender, tokenHash) {
    const holder = this.#get('SELECT sender FROM senders WHERE inbox = ? AND token_hash = ?', inbox, tokenHash);
    if (holder && holder.sender !== sender) throw new MailboxError('token_in_use', 409);
    const exists = this.#get('SELECT 1 AS found FROM senders WHERE inbox = ? AND sender = ?', inbox, sender);
    if (!exists && this.#get('SELECT COUNT(*) AS count FROM senders WHERE inbox = ?', inbox).count >= this.limits.maxSendersPerInbox) {
      throw new MailboxError('too_many_senders', 409);
    }
    this.#run(`INSERT INTO senders (inbox, sender, token_hash, created_at) VALUES (?, ?, ?, ?)
      ON CONFLICT (inbox, sender) DO UPDATE SET token_hash = excluded.token_hash`, inbox, sender, tokenHash, this.now());
  }

  // Removes a sender and what it left: an owner revoking a link wants none of it.
  deleteSender(inbox, sender) {
    return this.#transaction(() => {
      this.#release(this.#all('DELETE FROM envelopes WHERE inbox = ? AND sender = ? RETURNING size', inbox, sender));
      return this.#run('DELETE FROM senders WHERE inbox = ? AND sender = ?', inbox, sender).changes > 0;
    });
  }

  listSenders(inbox) {
    return this.#all(`SELECT s.sender, s.last_sent_at,
        (SELECT COUNT(*) FROM envelopes e WHERE e.inbox = s.inbox AND e.sender = s.sender AND e.expires_at > ?) AS pending
      FROM senders s WHERE s.inbox = ? ORDER BY s.sender`, this.now(), inbox)
      .map((row) => ({ sender: row.sender, pending: Number(row.pending), last_sent_at: row.last_sent_at === null ? null : Number(row.last_sent_at) }));
  }

  senderFor(inbox, tokenHash) {
    return this.#get('SELECT sender FROM senders WHERE inbox = ? AND token_hash = ?', inbox, tokenHash)?.sender;
  }

  // Stores an envelope; a sender's repeated idempotency key returns the
  // envelope it already left while that one is pending.
  put(inbox, sender, body, idempotencyKey = null) {
    const size = body.length;
    if (size === 0) throw new MailboxError('empty_envelope', 400);
    if (size > this.limits.maxEnvelopeBytes) throw new MailboxError('envelope_too_large', 413);
    const now = this.now();
    return this.#transaction(() => {
      if (idempotencyKey !== null) {
        const earlier = this.#get('SELECT seq, expires_at FROM envelopes WHERE inbox = ? AND sender = ? AND idempotency_key = ?',
          inbox, sender, idempotencyKey);
        if (earlier && earlier.expires_at > now) return { seq: Number(earlier.seq), expires_at: Number(earlier.expires_at), duplicate: true };
        if (earlier) this.#release(this.#all('DELETE FROM envelopes WHERE inbox = ? AND seq = ? RETURNING size', inbox, earlier.seq));
      }
      const held = this.#get('SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM envelopes WHERE inbox = ? AND expires_at > ?',
        inbox, now);
      if (held.count >= this.limits.maxPendingPerInbox || held.bytes + size > this.limits.maxPendingBytesPerInbox) {
        throw new MailboxError('inbox_full', 429);
      }
      const backlog = this.#get('SELECT COUNT(*) AS count FROM envelopes WHERE inbox = ? AND sender = ? AND expires_at > ?',
        inbox, sender, now);
      if (backlog.count >= this.limits.maxPendingPerSender) throw new MailboxError('sender_backlog', 429);
      if (this.totalBytes + this.pendingBytes + size > this.limits.maxTotalBytes) throw new MailboxError('mailbox_full', 507);
      const expiresAt = now + this.limits.ttlSeconds;
      const { seq } = this.#get('UPDATE inboxes SET last_seq = last_seq + 1 WHERE id = ? RETURNING last_seq AS seq', inbox);
      this.#run(`INSERT INTO envelopes (inbox, seq, sender, idempotency_key, received_at, expires_at, size, body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)`, inbox, seq, sender, idempotencyKey, now, expiresAt, size, body);
      this.#run('UPDATE senders SET last_sent_at = ? WHERE inbox = ? AND sender = ?', now, inbox, sender);
      this.pendingBytes += size;
      return { seq: Number(seq), expires_at: expiresAt, duplicate: false };
    });
  }

  // Pending envelopes after `after`, oldest first. Rows are read one at a
  // time, so a page that fills its byte budget early loads no more bodies.
  list(inbox, { after = 0, limit = 50, maxBytes = MAX_PAGE_BYTES } = {}) {
    const page = [];
    let bytes = 0;
    for (const row of this.#statement(`SELECT seq, sender, received_at, body FROM envelopes
        WHERE inbox = ? AND seq > ? AND expires_at > ? ORDER BY seq LIMIT ?`).iterate(inbox, after, this.now(), limit)) {
      if (page.length && bytes + row.body.length > maxBytes) break;
      bytes += row.body.length;
      page.push({ seq: Number(row.seq), sender: row.sender, received_at: Number(row.received_at), body: Buffer.from(row.body) });
    }
    return page;
  }

  // Deletes the envelopes up to and including `through`, and those in `seqs`.
  ack(inbox, { through, seqs = [] } = {}) {
    return this.#transaction(() => {
      const removed = through === undefined ? [] : this.#all('DELETE FROM envelopes WHERE inbox = ? AND seq <= ? RETURNING size', inbox, through);
      for (const seq of seqs) removed.push(...this.#all('DELETE FROM envelopes WHERE inbox = ? AND seq = ? RETURNING size', inbox, seq));
      this.#release(removed);
      return removed.length;
    });
  }

  // Drops expired envelopes, and the inboxes their owners stopped reading
  // with everything they hold, all in one transaction.
  prune() {
    const now = this.now();
    const idleSince = now - this.limits.inboxIdleDays * DAY_SECONDS;
    const idle = 'inbox IN (SELECT id FROM inboxes WHERE last_seen_at <= ?)';
    return this.#transaction(() => {
      const envelopes = this.#drop('expires_at <= ?', now) + this.#drop(idle, idleSince);
      this.#run(`DELETE FROM senders WHERE ${idle}`, idleSince);
      const inboxes = this.#run('DELETE FROM inboxes WHERE last_seen_at <= ?', idleSince).changes;
      return { envelopes, inboxes: Number(inboxes) };
    });
  }

  // A new file gets the tables. One from another version is refused, not
  // changed: no client used the first release, so nothing in it needs
  // keeping, and a newer one may hold what this version can't read.
  #createSchema() {
    const version = Number(this.#get('PRAGMA user_version').user_version);
    if (version > SCHEMA_VERSION) {
      throw new Error(`DATA_PATH holds a database from a newer mailbox (schema ${version}, this one knows ${SCHEMA_VERSION}): run that version.`);
    }
    if (version === 0 && this.#get("SELECT 1 AS found FROM sqlite_master WHERE type = 'table' AND name = 'inboxes'")) {
      throw new Error('DATA_PATH holds a database from the mailbox\'s first release, which numbered envelopes across the whole mailbox; '
        + 'this one numbers them per inbox. No client used that release, so nothing in it needs keeping: move it aside, with its -wal '
        + 'and -shm files, and start again.');
    }
    this.db.exec(SCHEMA);
    this.db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
  }

  // Rows deleted inside the current transaction, as they come back from RETURNING.
  #release(rows) {
    for (const row of rows) this.pendingBytes -= Number(row.size);
    return rows;
  }

  // Deletes the envelopes `where` picks inside the current transaction,
  // summing their sizes rather than reading every row back; how many.
  #drop(where, ...values) {
    this.pendingBytes -= Number(this.#get(`SELECT COALESCE(SUM(size), 0) AS bytes FROM envelopes WHERE ${where}`, ...values).bytes);
    return Number(this.#run(`DELETE FROM envelopes WHERE ${where}`, ...values).changes);
  }

  #transaction(work) {
    this.db.exec('BEGIN IMMEDIATE');
    this.pendingBytes = 0;
    try {
      const result = work();
      this.db.exec('COMMIT');
      this.totalBytes += this.pendingBytes;
      return result;
    } catch (error) {
      try {
        this.db.exec('ROLLBACK');
      } catch {
        // SQLite already undid it (as it does when the disk is full): the first error says why.
      }
      throw error;
    } finally {
      this.pendingBytes = undefined;
    }
  }

  #statement(sql) {
    let statement = this.statements.get(sql);
    if (!statement) this.statements.set(sql, statement = this.db.prepare(sql));
    return statement;
  }

  #get(sql, ...values) {
    return this.#statement(sql).get(...values);
  }

  #all(sql, ...values) {
    return this.#statement(sql).all(...values);
  }

  #run(sql, ...values) {
    return this.#statement(sql).run(...values);
  }
}
