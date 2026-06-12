"""
End-to-end funnel: phrase candidates -> available phrase domains.

Tiered funnel (cheap filters first, authoritative confirmation last):

  1. candidates.jsonl            (from candidates.py: phrase x word-TLD)
  2. DNS cull        (optional)  has NS records? -> registered, drop it.
                                 Cheap (hundreds/sec) and POLITE: it spares the
                                 registries an RDAP query for already-taken
                                 names. NEVER treats "no NS" as available.
  3. RDAP scan       (primary)   404 = available, 200 = taken. Per-registry
                                 adaptive pacing; resumable; deadline-bounded.
  4. Price confirm   (optional)  registrar API -> premium flag + renew price.

Outputs results/available_domains.{csv,txt} -- the deliverable.

Run the full job politely, under a wall-clock budget:
  python candidates.py --source norvig --min-zipf-w1 3.0
  python scan.py --candidates candidates.jsonl --dns-cull \
                 --base-interval 1.5 --deadline-seconds 18000
"""

import argparse
import asyncio
import concurrent.futures
import csv
import json
import os
import sys

import rdap


def load_candidates(path, limit=0):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


# --- stage 2: DNS cull --------------------------------------------------------

_RESOLVER = None


def _get_resolver():
    global _RESOLVER
    if _RESOLVER is None:
        import dns.resolver
        r = dns.resolver.Resolver(configure=True)
        r.lifetime = 5.0
        r.timeout = 3.0
        _RESOLVER = r
    return _RESOLVER


def _resolves(domain):
    """True  -> has NS (delegated): almost certainly registered, safe to drop.
    False/None -> not clearly registered: KEEP for authoritative RDAP check.
    We only ever act on the positive signal; absence of records is inconclusive.
    """
    try:
        import dns.resolver
        try:
            ans = _get_resolver().resolve(domain, "NS")
            return len(ans) > 0
        except dns.resolver.NXDOMAIN:
            return False                 # not in DNS -> let RDAP decide
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return None                  # ambiguous -> keep
        except Exception:
            return None
    except ImportError:
        import socket                    # stdlib fallback: A records only
        try:
            socket.getaddrinfo(domain, None)
            return True
        except socket.gaierror:
            return None
        except Exception:
            return None


def dns_cull(candidates, workers=50):
    """Drop candidates that are clearly delegated (taken). -> (survivors, taken)."""
    survivors, taken = [], []
    done = 0
    total = len(candidates)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for c, res in ex.map(lambda c: (c, _resolves(c["domain"])), candidates):
            (taken if res is True else survivors).append(c)
            done += 1
            if done % 500 == 0:
                print(f"  DNS cull {done}/{total} ...", file=sys.stderr)
    print(f"DNS cull: {len(taken)} clearly taken (have NS), "
          f"{len(survivors)} survive to RDAP", file=sys.stderr)
    return survivors, taken


# --- stage 4: price confirm ---------------------------------------------------

def confirm_prices(available, provider, max_create, max_renew):
    from registrar_check import get_checker, within_budget
    checker = get_checker(provider)
    domains = [c["domain"] for c in available]
    print(f"price-checking {len(domains)} names via {provider} ...",
          file=sys.stderr)
    priced = {p.domain: p for p in checker.check(domains)}
    out = []
    for c in available:
        p = priced.get(c["domain"])
        if p is None:
            continue
        out.append({**c, "purchasable": p.purchasable,
                    "premium_confirmed": p.premium,
                    "create_price": p.create_price, "renew_price": p.renew_price,
                    "within_budget": within_budget(p, max_create, max_renew)})
    return out


# --- export -------------------------------------------------------------------

def export(available, outdir="results"):
    os.makedirs(outdir, exist_ok=True)
    rows = sorted(available, key=lambda r: -r.get("count", 0))
    csv_path = os.path.join(outdir, "available_domains.csv")
    txt_path = os.path.join(outdir, "available_domains.txt")
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        # rank = stable position in the commonality sort, so the original order
        # is recoverable after the user sorts/filters in a spreadsheet.
        w.writerow(["rank", "domain", "word1", "tld", "phrase_count",
                    "premium_likely", "restricted"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r["domain"], r["w1"], r["w2"], r.get("count", 0),
                        int(bool(r.get("premium"))), int(bool(r.get("restricted")))])
    with open(txt_path, "w") as fh:
        for r in rows:
            fh.write(r["domain"] + "\n")
    print(f"\nwrote {len(rows)} available domains -> {csv_path}", file=sys.stderr)
    print(f"           plain list -> {txt_path}", file=sys.stderr)
    return csv_path, txt_path


# --- orchestration ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Scan phrase domains for availability")
    ap.add_argument("--candidates", default="candidates.jsonl")
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dns-cull", action="store_true",
                    help="cheap DNS pre-filter to drop clearly-taken names")
    ap.add_argument("--inflight", type=int, default=60,
                    help="max concurrent HTTP requests across all hosts")
    ap.add_argument("--base-interval", type=float, default=1.0,
                    help="polite starting seconds between queries to one host")
    ap.add_argument("--deadline-seconds", type=float, default=None,
                    help="stop starting RDAP queries after N s (resumable)")
    ap.add_argument("--confirm-price", action="store_true")
    ap.add_argument("--provider", default="namecom")
    ap.add_argument("--max-create", type=float, default=None)
    ap.add_argument("--max-renew", type=float, default=None)
    args = ap.parse_args()

    all_cands = load_candidates(args.candidates, args.limit)
    print(f"loaded {len(all_cands)} candidates", file=sys.stderr)

    survivors = all_cands
    if args.dns_cull:
        survivors, _ = dns_cull(all_cands)

    results = asyncio.run(rdap.run(survivors, args.out, inflight=args.inflight,
                                   base_interval=args.base_interval,
                                   deadline_seconds=args.deadline_seconds))

    available = [c for c in all_cands if results.get(c["domain"]) == rdap.AVAILABLE]

    if args.confirm_price and available:
        confirmed = confirm_prices(available, args.provider,
                                   args.max_create, args.max_renew)
        buyable = [c for c in confirmed if c.get("within_budget")]
        export(buyable)
        print(f"\n=== {len(buyable)} BUYABLE (available + in budget) ===")
        for c in sorted(buyable, key=lambda x: x.get("renew_price") or 0)[:60]:
            print(f"  {c['domain']:<26} renew=${c.get('renew_price')}")
    else:
        export(available)
        print(f"\n=== {len(available)} AVAILABLE via RDAP "
              f"(price unconfirmed; premium flagged) ===")
        for c in sorted(available, key=lambda x: -x["count"])[:60]:
            flags = ",".join(f for f, on in
                             [("PREMIUM-likely", c.get("premium")),
                              ("restricted", c.get("restricted"))] if on)
            print(f"  {c['domain']:<26} phrase_count={c['count']:>12}"
                  f"{('  [' + flags + ']') if flags else ''}")


if __name__ == "__main__":
    main()
