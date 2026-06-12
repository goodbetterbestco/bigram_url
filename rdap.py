"""
RDAP-based availability checker -- polite, adaptive, resumable.

RDAP (RFC 7480-9) is the modern, structured replacement for WHOIS. ICANN
*mandates* an RDAP server for every gTLD, so for the word-gTLDs we care about
(.live, .zone, .world, ...) coverage is excellent and the signal is clean:

    GET {rdap_base}/domain/{name}
      HTTP 404  -> NOT registered  => AVAILABLE
      HTTP 200  -> registered      => taken
      HTTP 429  -> rate limited     => honor Retry-After, slow this host, retry
      other/timeout -> UNKNOWN      => recheck later

Politeness is the whole design here (we must not hammer registries):
  * Per-host serial pacing: each registry's RDAP host is queried no faster than
    its current interval, with jitter. Hosts are independent, so many run in
    parallel without any single registry seeing a burst.
  * ADAPTIVE backoff: a 429 from a host multiplicatively increases that host's
    interval (and we obey Retry-After exactly); sustained success slowly relaxes
    it. The scanner self-tunes to each registry's real limit instead of guessing.
  * Resumable + deadline: results are checkpointed to JSONL as we go, so a run
    can stop at a deadline (to stay under a wall-clock budget) and resume later
    without re-querying anything.

Caveat (see README): RDAP says nothing about *price*. A 404 can still be a
registry-premium name or a reserved label; closed dot-brand and credential-gated
TLDs are excluded upstream in candidates.py / tld_words.py.
"""

import argparse
import asyncio
import email.utils
import json
import os
import random
import time
from collections import defaultdict

try:
    import aiohttp
except ImportError:
    raise SystemExit("pip install aiohttp  (see requirements.txt)")

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_CACHE = ".rdap_bootstrap.json"
USER_AGENT = "phrase-domain-scanner/1.0 (research; non-commercial; polite)"

AVAILABLE = "available"
TAKEN = "taken"
UNKNOWN = "unknown"
NO_RDAP = "no_rdap_server"
SKIPPED = "skipped_deadline"


async def load_bootstrap(session, max_age=86400):
    """tld -> rdap base url, from IANA bootstrap (cached on disk)."""
    if os.path.exists(BOOTSTRAP_CACHE) and (
        time.time() - os.path.getmtime(BOOTSTRAP_CACHE) < max_age
    ):
        with open(BOOTSTRAP_CACHE) as fh:
            data = json.load(fh)
    else:
        async with session.get(BOOTSTRAP_URL) as resp:
            data = await resp.json(content_type=None)
        with open(BOOTSTRAP_CACHE, "w") as fh:
            json.dump(data, fh)

    mapping = {}
    for entry in data.get("services", []):
        tlds, urls = entry[0], entry[1]
        base = next((u for u in urls if u.startswith("https")), urls[0])
        for tld in tlds:
            mapping[tld.lower()] = base.rstrip("/")
    return mapping


def _parse_retry_after(value):
    """Retry-After may be seconds or an HTTP date. Return seconds (float)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    dt = email.utils.parsedate_to_datetime(value)
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


class AdaptiveThrottle:
    """Per-host serial pacer that backs off on 429 and relaxes on success.

    Each host has its own current interval. We never issue two requests to the
    same host closer together than that interval (+ jitter). A 429 doubles the
    interval (capped); a streak of successes nudges it back toward the floor.
    """

    def __init__(self, base_interval=1.0, min_interval=0.5, max_interval=120.0):
        self.base = base_interval
        self.floor = min_interval
        self.ceil = max_interval
        self.interval = defaultdict(lambda: base_interval)
        self._next = defaultdict(float)
        self._ok_streak = defaultdict(int)
        self._locks = defaultdict(asyncio.Lock)

    async def wait(self, host):
        async with self._locks[host]:
            now = time.monotonic()
            if self._next[host] > now:
                await asyncio.sleep(self._next[host] - now)
            now = time.monotonic()
            jitter = self.interval[host] * random.uniform(0.0, 0.25)
            self._next[host] = now + self.interval[host] + jitter

    def on_429(self, host, retry_after=None):
        self._ok_streak[host] = 0
        self.interval[host] = min(self.ceil, max(self.interval[host] * 2,
                                                 self.base * 2))
        if retry_after is not None:
            # do not query this host again until retry_after has elapsed
            self._next[host] = max(self._next[host],
                                   time.monotonic() + retry_after)

    def on_success(self, host):
        self._ok_streak[host] += 1
        if self._ok_streak[host] >= 20 and self.interval[host] > self.floor:
            self.interval[host] = max(self.floor, self.interval[host] * 0.8)
            self._ok_streak[host] = 0


async def check_one(session, domain, rdap_base, throttle, retries=4):
    if not rdap_base:
        return NO_RDAP, None
    host = rdap_base.split("/")[2]
    url = f"{rdap_base}/domain/{domain}"
    for attempt in range(retries + 1):
        await throttle.wait(host)
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 404:
                    throttle.on_success(host)
                    return AVAILABLE, 404
                if resp.status == 200:
                    throttle.on_success(host)
                    return TAKEN, 200
                if resp.status in (429, 503):
                    ra = _parse_retry_after(resp.headers.get("Retry-After"))
                    throttle.on_429(host, ra)
                    continue
                # 400/403/422/5xx etc -> registry quirk; treat as unknown
                return UNKNOWN, resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # transient network error: brief backoff, then retry
            await asyncio.sleep(min(30.0, 2.0 ** attempt))
    return UNKNOWN, None


def load_done(out_path):
    done = {}
    if os.path.exists(out_path):
        with open(out_path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    # a deadline-skipped record is NOT done; let it retry
                    if r.get("status") != SKIPPED:
                        done[r["domain"]] = r["status"]
                except (json.JSONDecodeError, KeyError):
                    pass
    return done


async def run(candidates, out_path, concurrency=40, per_host_interval=1.0,
              timeout=20, deadline_seconds=None):
    """candidates: list of dicts with at least {'domain','w1','w2'}.

    Resumable: anything already in out_path with a real status is skipped.
    deadline_seconds: stop starting new queries after this many seconds (the
    in-flight ones finish); remaining candidates are left for a resume.
    """
    done = load_done(out_path)
    todo = [c for c in candidates if c["domain"] not in done]
    print(f"{len(candidates)} candidates, {len(done)} already done, "
          f"{len(todo)} to check")
    if not todo:
        return done

    throttle = AdaptiveThrottle(base_interval=per_host_interval)
    sem = asyncio.Semaphore(concurrency)
    conn = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=600)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rdap+json"}
    to = aiohttp.ClientTimeout(total=timeout)

    counts = defaultdict(int)
    start = time.monotonic()
    deadline = start + deadline_seconds if deadline_seconds else None

    try:
        from tqdm import tqdm
        bar = tqdm(total=len(todo), unit="dom", smoothing=0.05)
    except ImportError:
        bar = None

    async with aiohttp.ClientSession(connector=conn, headers=headers,
                                     timeout=to) as session:
        rdap_map = await load_bootstrap(session)
        out = open(out_path, "a", encoding="utf-8")
        lock = asyncio.Lock()

        async def worker(c):
            async with sem:
                if deadline and time.monotonic() > deadline:
                    status, code = SKIPPED, None
                else:
                    base = rdap_map.get(c["w2"])
                    status, code = await check_one(session, c["domain"], base,
                                                   throttle)
            rec = {**c, "status": status, "http": code}
            async with lock:
                if status != SKIPPED:
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                counts[status] += 1
                if bar:
                    bar.update(1)
                    bar.set_postfix(avail=counts[AVAILABLE], taken=counts[TAKEN],
                                    unk=counts[UNKNOWN] + counts[NO_RDAP],
                                    refresh=False)
            return rec

        await asyncio.gather(*(worker(c) for c in todo))
        out.close()

    if bar:
        bar.close()
    elapsed = time.monotonic() - start
    print(f"done in {elapsed/60:.1f} min: {dict(counts)}")
    if deadline and counts[SKIPPED]:
        print(f"NOTE: hit deadline; {counts[SKIPPED]} left -- rerun to resume.")
    return load_done(out_path)


def main():
    ap = argparse.ArgumentParser(description="Polite RDAP availability scan")
    ap.add_argument("--candidates", default="candidates.jsonl")
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--per-host-interval", type=float, default=1.0,
                    help="seconds between requests to the SAME registry (polite "
                         "floor; auto-increases on 429)")
    ap.add_argument("--deadline-seconds", type=float, default=None,
                    help="stop starting queries after N seconds (resumable)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cands = []
    with open(args.candidates) as fh:
        for line in fh:
            line = line.strip()
            if line:
                cands.append(json.loads(line))
    if args.limit:
        cands = cands[: args.limit]

    results = asyncio.run(run(cands, args.out, args.concurrency,
                              args.per_host_interval,
                              deadline_seconds=args.deadline_seconds))

    avail = sorted(d for d, s in results.items() if s == AVAILABLE)
    print(f"\n=== {len(avail)} AVAILABLE phrase domains ===")
    for d in avail[:60]:
        print("  ", d)
    if len(avail) > 60:
        print(f"  ... and {len(avail) - 60} more in {args.out}")


if __name__ == "__main__":
    main()
