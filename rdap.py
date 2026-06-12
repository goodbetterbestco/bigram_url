"""
RDAP-based availability checker -- polite, host-sharded, adaptive, resumable.

RDAP (RFC 7480-9) is the structured replacement for WHOIS; ICANN mandates an
RDAP server for every gTLD, so the word-gTLDs we care about have clean
semantics:

    GET {rdap_base}/domain/{name}
      404 -> not registered  => AVAILABLE
      200 -> registered      => taken
      429/503 -> rate limited => honor Retry-After, slow THIS host, retry
      other/timeout -> UNKNOWN (retryable on a later run)

KEY FACT that drives the design: the ~376 word-gTLDs are operated by only ~54
RDAP back-ends, and ONE of them (Identity Digital) runs the majority of them
(.life/.world/.live/.news/.zone/...). So the work does NOT fan out evenly --
most of it funnels through a few hosts. Politeness therefore means pacing
*per host*, and the wall-clock is bounded by the busiest host's queue.

Design:
  * Shard candidates by RDAP host. One drain() coroutine per host walks that
    host's queue serially, sleeping `interval` between requests (adaptive).
  * A global semaphore caps the number of in-flight HTTP requests, and is held
    ONLY around the GET -- never during a sleep. So a slow/throttled host never
    blocks the others (the bug that sank the first version).
  * Adaptive pacing: start at base_interval; a 429 doubles this host's interval
    (capped) and obeys Retry-After; a streak of successes relaxes it toward a
    floor. Each registry self-tunes to its own tolerance.
  * Resumable + deadline-bounded: only definitive results (available/taken) are
    treated as done; a run can stop at a deadline and resume without re-querying.

Caveat (see README): RDAP says nothing about price. Premium/reserved names and
closed dot-brand / credential-gated TLDs are handled upstream in tld_words.py.
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

# statuses we consider definitive (won't change on a re-run)
_DONE = {AVAILABLE, TAKEN}


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
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    dt = email.utils.parsedate_to_datetime(value)
    return max(0.0, dt.timestamp() - time.time()) if dt else None


def load_done(out_path):
    """Map domain -> status, counting only DEFINITIVE results as done so that
    unknown/no-rdap rows are retried on a resume."""
    done = {}
    if os.path.exists(out_path):
        with open(out_path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    if r.get("status") in _DONE:
                        done[r["domain"]] = r["status"]
                except (json.JSONDecodeError, KeyError):
                    pass
    return done


async def run(candidates, out_path, inflight=60, base_interval=1.0,
              min_interval=1.0, max_interval=30.0, timeout=12,
              max_retries=3, retry_after_cap=90.0, host_budget=6000.0,
              max_unknown_streak=8, deadline_seconds=None):
    """Scan candidates (list of dicts with 'domain','w1','w2') via RDAP.

    Host-sharded: each RDAP back-end drains its own queue at a polite, steady
    pace (default 1 req/sec/host). Wall-clock is governed by the busiest host
    (Identity Digital, which runs most word-gTLDs).

    Three guardrails make a single misbehaving host unable to stall the run:
      * retry_after_cap : never honor a 429 Retry-After longer than this in one
        sleep (a registry that penalty-boxes us for an hour must not hang us).
      * host_budget     : max seconds any one host's drain may run, then it
        stops and leaves the rest for a resume.
      * a hard per-request timeout wraps every GET.
    """
    done = load_done(out_path)
    todo = [c for c in candidates if c["domain"] not in done]
    print(f"{len(candidates)} candidates, {len(done)} already done, "
          f"{len(todo)} to check")
    if not todo:
        return done

    sem = asyncio.Semaphore(inflight)
    conn = aiohttp.TCPConnector(limit=inflight, ttl_dns_cache=600)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rdap+json"}
    to = aiohttp.ClientTimeout(total=timeout)

    counts = defaultdict(int)
    loop = asyncio.get_event_loop()
    start = loop.time()
    deadline = start + deadline_seconds if deadline_seconds else None

    try:
        from tqdm import tqdm
        bar = tqdm(total=len(todo), unit="dom", smoothing=0.02)
    except ImportError:
        bar = None

    async with aiohttp.ClientSession(connector=conn, headers=headers,
                                     timeout=to) as session:
        rdap_map = await load_bootstrap(session)

        by_host = defaultdict(list)
        no_rdap = []
        for c in todo:
            base = rdap_map.get(c["w2"])
            (no_rdap.append(c) if not base else by_host[base].append((c, base)))

        out = open(out_path, "a", encoding="utf-8")
        lock = asyncio.Lock()

        async def emit(rec):
            async with lock:
                out.write(json.dumps(rec) + "\n")
                out.flush()
                counts[rec["status"]] += 1
                if bar:
                    bar.update(1)
                    bar.set_postfix(avail=counts[AVAILABLE], taken=counts[TAKEN],
                                    unk=counts[UNKNOWN], refresh=False)

        for c in no_rdap:
            await emit({**c, "status": NO_RDAP, "http": None})

        async def fetch(domain, base):
            """One GET. Returns (status, code, retry_after). Holds the network
            slot only for the request itself."""
            url = f"{base}/domain/{domain}"
            async with sem:
                try:
                    async with session.get(url, allow_redirects=True) as r:
                        if r.status == 404:
                            return AVAILABLE, 404, None
                        if r.status == 200:
                            return TAKEN, 200, None
                        if r.status in (429, 503):
                            return "_retry", r.status, _parse_retry_after(
                                r.headers.get("Retry-After"))
                        return UNKNOWN, r.status, None
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return "_err", None, None

        async def drain(base, items):
            host = base.split("/")[2]
            interval = base_interval
            ok_streak = 0
            unk_streak = 0
            host_start = loop.time()
            for c, _ in items:
                now = loop.time()
                if deadline and now > deadline:
                    break  # global deadline: leave the rest for a resume
                if now - host_start > host_budget:
                    break  # this host took too long; defer rest to a resume
                if unk_streak >= max_unknown_streak:
                    # host is persistently rate-limiting us -- stop poking it,
                    # defer the rest to a later (spaced-out) resume. Polite.
                    break
                status = code = None
                for attempt in range(max_retries + 1):
                    try:
                        status, code, ra = await asyncio.wait_for(
                            fetch(c["domain"], base), timeout=timeout + 5)
                    except asyncio.TimeoutError:
                        status, code, ra = "_err", None, None
                    if status == "_retry":
                        # back off this host; honor Retry-After but CAP it so a
                        # long penalty-box can never hang the run.
                        interval = min(max_interval, max(interval * 2,
                                                         base_interval * 2))
                        ok_streak = 0
                        wait = ra if ra is not None else interval
                        await asyncio.sleep(min(retry_after_cap, wait))
                        continue
                    if status == "_err":
                        await asyncio.sleep(min(15.0, 2.0 ** attempt))
                        continue
                    break  # definitive (available/taken/unknown-http)
                if status in ("_retry", "_err"):
                    status, code = UNKNOWN, code
                    unk_streak += 1
                else:
                    unk_streak = 0
                    ok_streak += 1
                    if ok_streak >= 40 and interval > min_interval:
                        interval = max(min_interval, interval * 0.85)
                        ok_streak = 0
                await emit({**c, "status": status, "http": code})
                # pace before the next request to THIS host (no slot held)
                await asyncio.sleep(interval * random.uniform(0.9, 1.25))

        sizes = sorted((len(v) for v in by_host.values()), reverse=True)
        print(f"sharded over {len(by_host)} RDAP hosts; "
              f"busiest holds {sizes[0] if sizes else 0} names "
              f"(this bounds the wall-clock)")

        await asyncio.gather(*(drain(b, items) for b, items in by_host.items()))
        out.close()

    if bar:
        bar.close()
    elapsed = loop.time() - start
    print(f"done in {elapsed/60:.1f} min: {dict(counts)}")
    remaining = len(todo) - sum(counts.values())
    if deadline and remaining > 0:
        print(f"NOTE: deadline hit; ~{remaining} unqueried -- rerun to resume.")
    return load_done(out_path)


def main():
    ap = argparse.ArgumentParser(description="Polite host-sharded RDAP scan")
    ap.add_argument("--candidates", default="candidates.jsonl")
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--inflight", type=int, default=60,
                    help="max concurrent HTTP requests across all hosts")
    ap.add_argument("--base-interval", type=float, default=1.0,
                    help="starting seconds between requests to the SAME host")
    ap.add_argument("--deadline-seconds", type=float, default=None)
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

    results = asyncio.run(run(cands, args.out, inflight=args.inflight,
                              base_interval=args.base_interval,
                              deadline_seconds=args.deadline_seconds))
    avail = sorted(d for d, s in results.items() if s == AVAILABLE)
    print(f"\n=== {len(avail)} AVAILABLE phrase domains ===")
    for d in avail[:60]:
        print("  ", d)
    if len(avail) > 60:
        print(f"  ... and {len(avail) - 60} more in {args.out}")


if __name__ == "__main__":
    main()
