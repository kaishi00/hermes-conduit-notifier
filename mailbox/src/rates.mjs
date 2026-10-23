// Fixed windows counted per key. A map given `maxBuckets` stops counting
// new keys once it holds that many live ones: they pass uncounted until a
// sweep frees room, so a flood of fresh keys can neither grow the map nor
// lock anyone out.
export class RateLimits {
  constructor(maxBuckets = Infinity) {
    this.maxBuckets = maxBuckets;
    this.buckets = new Map();
    this.sweptAt = 0;
  }

  // Counts one more for `key`; false once it is past `maximum` this window.
  take(key, maximum, windowMs, now = Date.now()) {
    const full = this.buckets.size >= this.maxBuckets;
    if ((full || this.buckets.size > 10_000) && now - this.sweptAt > (full ? 5_000 : 30_000)) {
      this.sweptAt = now;
      for (const [seen, bucket] of this.buckets) if (bucket.resetAt <= now) this.buckets.delete(seen);
    }
    let bucket = this.buckets.get(key);
    if (!bucket || bucket.resetAt <= now) {
      if (!bucket && this.buckets.size >= this.maxBuckets) return true;
      bucket = { count: 0, resetAt: now + windowMs };
      this.buckets.set(key, bucket);
    }
    bucket.count += 1;
    return bucket.count <= maximum;
  }

  // Whether `key` has used all of `maximum` this window, without counting.
  exceeded(key, maximum, now = Date.now()) {
    const bucket = this.buckets.get(key);
    return Boolean(bucket && bucket.resetAt > now && bucket.count >= maximum);
  }
}
