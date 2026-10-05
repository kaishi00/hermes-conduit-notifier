// Accepted event IDs, kept only in memory. They exist to stop a repeated
// delivery of the same event within 24 hours, which makes them unlike the
// rest of the store: losing them on restart costs at most one duplicate push
// (the plugin never retries a send), while persisting them made every event
// rewrite the whole data file. Both bounds forget old IDs rather than reject
// new events, so a busy relay never drops a notification to make room.
export const EVENT_ID_TTL_MS = 24 * 60 * 60_000;
const SWEEP_INTERVAL_MS = 60_000;

export class EventLedger {
  constructor({ perInstallation, total }) {
    for (const bound of [perInstallation, total]) {
      if (!Number.isSafeInteger(bound) || bound <= 0) throw new TypeError('Event ID bounds must be positive integers.');
    }
    this.perInstallation = perInstallation;
    this.total = total;
    // installationId -> Map(`${gatewayId}:${eventId}` -> acceptedAt), each
    // inner map in acceptance order so its first entry is its oldest.
    this.byInstallation = new Map();
    this.size = 0;
    this.lastSweepAt = 0;
  }

  has(installationId, gatewayId, eventId, now = Date.now()) {
    const acceptedAt = this.byInstallation.get(installationId)?.get(eventKey(gatewayId, eventId));
    return acceptedAt !== undefined && acceptedAt >= now - EVENT_ID_TTL_MS;
  }

  // Records an accepted event ID; false means it was already recorded.
  add(installationId, gatewayId, eventId, acceptedAt = Date.now()) {
    this.sweep(Date.now());
    const key = eventKey(gatewayId, eventId);
    let events = this.byInstallation.get(installationId);
    const previous = events?.get(key);
    if (previous !== undefined) {
      if (previous >= acceptedAt - EVENT_ID_TTL_MS) return false;
      // Expired but not yet swept: re-record it as the newest entry.
      events.delete(key);
      this.size -= 1;
    }
    if (!events) {
      events = new Map();
      this.byInstallation.set(installationId, events);
    }
    // An installation at its bound forgets its own oldest ID, so a flood
    // from one installation never touches another installation's IDs.
    if (events.size >= this.perInstallation) this.dropOldest(events);
    events.set(key, acceptedAt);
    this.size += 1;
    if (this.size > this.total) this.trimLargest();
    return true;
  }

  countFor(installationId) {
    return this.byInstallation.get(installationId)?.size ?? 0;
  }

  // Upgrade path for data files written when event IDs were persisted
  // (`installationId:gatewayId:eventId` -> acceptedAt), so dedupe survives
  // the deploy that drops them from the file. Malformed entries are skipped.
  importPersisted(eventIds, now = Date.now()) {
    if (!eventIds || typeof eventIds !== 'object' || Array.isArray(eventIds)) return;
    const entries = [];
    for (const [key, value] of Object.entries(eventIds)) {
      const acceptedAt = Number(value);
      if (!Number.isFinite(acceptedAt) || acceptedAt < now - EVENT_ID_TTL_MS) continue;
      const first = key.indexOf(':');
      const second = first < 0 ? -1 : key.indexOf(':', first + 1);
      if (first <= 0 || second <= first + 1 || second === key.length - 1) continue;
      entries.push([key.slice(0, first), key.slice(first + 1, second), key.slice(second + 1), acceptedAt]);
    }
    entries.sort((left, right) => left[3] - right[3]);
    for (const [installationId, gatewayId, eventId, acceptedAt] of entries) this.add(installationId, gatewayId, eventId, acceptedAt);
  }

  sweep(now = Date.now()) {
    if (now - this.lastSweepAt < SWEEP_INTERVAL_MS) return;
    this.lastSweepAt = now;
    const cutoff = now - EVENT_ID_TTL_MS;
    // A full pass rather than stopping at the first live entry: a clock step
    // backwards can leave an expired ID behind a newer one.
    for (const [installationId, events] of this.byInstallation) {
      for (const [key, acceptedAt] of events) {
        if (acceptedAt >= cutoff) continue;
        events.delete(key);
        this.size -= 1;
      }
      if (!events.size) this.byInstallation.delete(installationId);
    }
  }

  dropOldest(events) {
    const oldest = events.keys().next();
    if (oldest.done) return;
    events.delete(oldest.value);
    this.size -= 1;
  }

  // Over the total bound the installation holding the most IDs gives up its
  // oldest ones: the heaviest sender pays for the space. It gives up 0.1% of
  // the bound beyond what is needed, so the scan for it runs once per batch
  // of events rather than on every event while the ledger stays full.
  trimLargest() {
    let largestId;
    let largest;
    for (const [installationId, events] of this.byInstallation) {
      if (!largest || events.size > largest.size) {
        largestId = installationId;
        largest = events;
      }
    }
    if (!largest) return;
    const excess = this.size - this.total + Math.floor(this.total / 1_000);
    for (let dropped = 0; dropped < excess && largest.size; dropped += 1) this.dropOldest(largest);
    if (!largest.size) this.byInstallation.delete(largestId);
  }
}

function eventKey(gatewayId, eventId) {
  return `${gatewayId}:${eventId}`;
}
