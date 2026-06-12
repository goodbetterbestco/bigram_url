# bigram_url

Find domains that spell a common two-word English phrase by splitting the phrase
across the dot. `word1` is the second-level name, `word2` is the TLD. The tool
reports which ones are currently available to register.

```
learn . how          free . delivery        music . video
  │      │
  │      └─ word2: a TLD that is also an English word (.how, .video, .review)
  └──────── word1: the second-level name
            "word1 word2" reads as a normal English phrase
```

This is a constrained [domain hack](https://en.wikipedia.org/wiki/Domain_hack).
The constraint is that the second word must be a dictionary word that is also a
top-level domain (`.now`, `.life`, `.world`, `.how`, `.video`, `.review`).

## Results

A full run found 3,985 available phrase-domains (snapshot in [`results/`](results/)),
sorted by phrase frequency. A few from the top:

| domain | phrase frequency |
|---|---:|
| `store.reviews` | 12.2M |
| `free.delivery` | 9.3M |
| `movies.online` | 8.4M |
| `personal.loan` | 2.5M |
| `music.video` | 1.8M |
| `much.fun` | 1.5M |

The obvious ones (`right.now`, `real.estate`) are taken or sit on brand-locked
TLDs. What remains available is the long tail. The full ranked list is in
[`results/available_domains.csv`](results/available_domains.csv).

## Search space

There are about 1,440 delegated TLDs in the DNS root, and about 376 of them are
usable English words. That bounds the problem. Instead of scanning the web, the
tool intersects two sets:

```
(common two-word phrases)  ∩  (phrases whose 2nd word is one of ~376 TLD-words)
```

Against Peter Norvig's 250k most-frequent web bigrams, the intersection is about
9,500 candidate domains, which is small enough to check in full.

## Quickstart

```bash
git clone https://github.com/goodbetterbestco/bigram_url
cd bigram_url
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. build candidates from the corpus (auto-downloads Norvig's 5 MB file)
python candidates.py --source norvig --min-zipf-w1 3.0

# 2. check availability via RDAP
python scan.py --candidates candidates.jsonl --dns-cull

# output: results/available_domains.{csv,txt}, sorted by phrase frequency
```

Smoke test with no downloads: `python candidates.py --source demo`.

## How it works

A funnel. Cheap filters run first, authoritative checks last.

```
 corpus bigrams ─► candidates.py ─► candidates.jsonl
                     • keep pairs whose 2nd word is a TLD-word
                     • strict DNS-label + ICANN-reserved-label + stopword filters
                            │
                            ▼
                         scan.py
   ┌────────────────────────┼─────────────────────────────────┐
   │ 1. DNS cull   │ has NS records? -> registered, drop it
   │ 2. RDAP scan  │ 404 = available, 200 = taken. Sharded per registry back-end
   │ 3. price (opt)│ registrar API -> premium flag + renewal price
   └────────────────────────┴─────────────────────────────────┘
                            ▼
                  results/available_domains.{csv,txt}
```

| file | role |
|---|---|
| `tld_words.py` / `data/word_tlds.tsv` | the ~376 English-word gTLDs and their open/premium/restricted/brand flags |
| `build_tld_words.py` | regenerate that list from the live IANA root zone |
| `candidates.py` | bigram corpus to validated `word1.word2` candidates |
| `rdap.py` | host-sharded, resumable RDAP availability checker |
| `registrar_check.py` | optional price/premium confirmation (Name.com adapter) |
| `scan.py` | the end-to-end funnel |

## Runtime

A full sweep takes about 30 to 40 minutes. The 376 word-gTLDs map to only about
54 RDAP back-ends, and one operator (Identity Digital) runs most of them. The
wall-clock is set by how fast you can query that one busy host within its rate
limit (default about 1 request per second, with backoff on HTTP 429), not by the
number of candidates. `rdap.py` shards by back-end and paces each one separately.
The DNS pre-cull drops most already-registered names before they reach a
registry.

## "Available" is not "buyable"

RDAP reports whether a name is registered, not whether you can buy it or what it
costs. Check these before spending money:

- Registry-premium pricing. Many one-word second-level names are priced as
  registry "premium", from hundreds to thousands of dollars per year, often with
  the premium renewal fixed for the life of the domain. RDAP does not expose
  price. The `premium_likely` column flags the common cases. `--confirm-price`
  with a registrar key returns real numbers.
- Brand and closed TLDs. Some generic-looking TLDs are brand-owned and closed to
  the public (`.now`, `.buy`, `.here`). These are excluded from candidates.
- Credential-gated TLDs (`.bank`, `.law`, `.pharmacy`) report "available" but
  reject registration at verification. Excluded by default.
- ccTLDs (`.io`, `.ai`, `.me`) are out of scope. This targets English-word
  gTLDs, and most ccTLDs do not run RDAP.

## Rate limiting

Registry RDAP and WHOIS endpoints have per-IP rate limits. `rdap.py` paces per
host, honors `Retry-After`, backs off on 429, stops querying a back-end that
keeps refusing, and groups shared back-ends such as CentralNic. For high-volume
availability checking, use a registrar API or EPP instead.

## Development

Requires Python 3.10 or newer. Install with dev extras and run the tests:

```bash
pip install -e ".[dev]"   # or: pip install -r requirements.txt && pip install pytest
pytest                    # unit tests for label validation, TLD flags, RDAP parsing
```

The tests cover the pure logic (candidate filtering, the open/premium/restricted
TLD flags, and the `Retry-After` and resume parsing in `rdap.py`) and run offline.

## Data sources and licenses

- TLDs: [IANA root zone](https://data.iana.org/TLD/tlds-alpha-by-domain.txt) (public).
- Bigrams: [Peter Norvig's `count_2w.txt`](https://norvig.com/ngrams/) (MIT),
  optionally [Google Books Ngrams](https://books.google.com/ngrams) (CC BY 3.0).
- Word frequency: [`wordfreq`](https://github.com/rspeer/wordfreq) (Apache-2.0 code).
- Availability: RDAP via the [IANA bootstrap](https://data.iana.org/rdap/dns.json).

The code is released under the MIT License. Third-party data keeps its own
license. Availability is a point-in-time snapshot and does not guarantee you can
register or afford a given name.
