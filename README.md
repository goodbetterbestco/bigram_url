# Phrase-domain finder

Find website addresses that spell a **common two-word English phrase** by
splitting the phrase across the dot — `word1` is the second-level name and
`word2` is the TLD — and that are currently **available to register**.

```
right . now      learn . how       free . delivery
  │      │
  │      └── word2  must be a real, delegated TLD that is an English word
  └───────── word1  any registrable label
            together "word1 word2" must be a natural English phrase
```

This is a constrained [domain hack](https://en.wikipedia.org/wiki/Domain_hack):
the twist is that **word2 must itself be a dictionary word that happens to be a
TLD** (`.now`, `.life`, `.world`, `.house`, `.show`, …).

## Why this is tractable (and fast)

The naïve framing — "search all possible domains" — is hopeless. The key
insight is that **the second word is drawn from a tiny fixed set**: there are
only ~1,440 delegated TLDs in the entire DNS root, and only **~376 of them are
usable English words**. So you never enumerate the web; you enumerate

```
(common two-word phrases)  ∩  (phrases whose 2nd word is one of ~376 TLD-words)
```

From Peter Norvig's 250,000 most-frequent web bigrams, that intersection is
only **~9,500 candidate domains across ~311 TLDs** (after dropping function-word
phrases and registry-reserved labels). That's small enough to check exhaustively
in minutes.

## The pipeline (a funnel: cheap filters first, authoritative last)

```
 corpus bigrams ──► candidates.py ──► candidates.jsonl
                       │  • keep only pairs whose 2nd word is a TLD-word
                       │  • strict DNS-label + ICANN-reserved-label validation
                       │  • drop function-word first-words, rare words
                       ▼
                    scan.py
   ┌───────────────────┼───────────────────────────────┐
   │ 1. DNS cull (opt) │ resolves? → taken, drop it. ~1000s/sec. Cheap.
   │ 2. RDAP scan      │ 404=available, 200=taken. Sharded per RDAP host. PRIMARY.
   │ 3. Price confirm  │ registrar API → premium flag + renewal price.
   │    (opt)          │ The ONLY signal that means "actually buyable".
   └───────────────────┴───────────────────────────────┘
                       ▼
                 results.jsonl  +  printed list of available phrase domains
```

### Files
| file | role |
|------|------|
| `tld_words.py` | the ~376 word-TLDs + open/premium/restricted/brand flags |
| `data/word_tlds.tsv` | the list, derived from the live IANA root zone |
| `build_tld_words.py` | regenerate that list from IANA + a frequency dictionary |
| `candidates.py` | bigram corpus → validated `word1.word2` candidates |
| `rdap.py` | async RDAP availability checker (IANA bootstrap, 404=free) |
| `registrar_check.py` | price/premium confirmation via a registrar API (Name.com adapter) |
| `scan.py` | orchestrates the whole funnel |

## Usage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. generate candidates from the real corpus (auto-downloads Norvig's 5MB file)
python candidates.py --source norvig --min-zipf-w1 3.0

# 2. check availability via RDAP (sharded across registries, polite throttling)
python scan.py --candidates candidates.jsonl --dns-cull

# 3. (optional) confirm buyability + price with a registrar API
NAMECOM_USER=... NAMECOM_TOKEN=... \
python scan.py --candidates candidates.jsonl --confirm-price --max-renew 50
```

Quick smoke test with zero downloads: `python candidates.py --source demo`.

## How long does it take?

The intuition "each TLD has its own RDAP server, so the work fans out over
hundreds of registries" is **wrong**, and assuming it cost me a 6-hour failed
run. In reality the ~376 word-gTLDs resolve to only **~54 RDAP back-ends**, and
**one of them — Identity Digital — operates the majority** of them
(.life/.world/.live/.news/.zone/.today/.city/...). Of 9,466 candidates, **5,698
hit that single host.** So the load funnels onto a few endpoints, and the
wall-clock is governed by the busiest one, paced politely.

Two consequences shaped the design:
1. **Shard by host, not by TLD.** `rdap.py` groups candidates by RDAP back-end
   and drains each host's queue with its own adaptive pacer. The global
   in-flight semaphore is held *only* around each HTTP request — never during a
   pacing sleep — so the dominant host can't stall the others. (The first
   version held a slot during the sleep; all slots jammed behind Identity
   Digital and throughput collapsed to ~0.01 domains/sec.)
2. **DNS pre-cull is a big lever.** `--dns-cull` drops names with NS records
   (already registered) before RDAP — here it removed **4,957 of 9,466**, so
   only ~4,500 names reach the registries at all.

Measured throughput (this machine): **~3.6 domains/sec** sustained, bursting to
30+/sec while the ~53 small hosts drain, then settling to the pace of the
Identity-Digital tail. A **full sweep of ~9,500 candidates completes in roughly
30–45 minutes** — bounded by politely metering the one dominant registry
(default 1 req/sec/host, relaxing to 2/sec on sustained success, backing off on
429), not by CPU or the candidate count.

Politeness levers / notes:
- Every host self-tunes: a 429 doubles that host's interval and we obey
  `Retry-After`; we never parallelize across IPs to evade caps.
- The run is **resumable** (only definitive available/taken count as done) and
  **deadline-bounded** (`--deadline-seconds`) so it can fit a wall-clock budget.
- A **registrar batch API** (Name.com: 50 names/call, 3000 calls/hr ⇒ ~150k
  names/hr) is faster *and* returns price/premium — at the cost of an account.

Bottom line: a **half-hour** job, because the search space is only ~10k names
and the real bottleneck is one registry's polite rate limit.

## Important caveats (RDAP "available" ≠ "buyable")

RDAP/WHOIS answer *"is it registered?"*, **not** *"can I buy it, and for how
much?"*. A name can RDAP-404 yet be unbuyable. The scanner flags these; the
price-confirm stage resolves them:

- **Registry-premium pricing.** Many one-word second-level names are registry
  "premium" — `$100s–$1000s/yr`, often with a **premium renewal locked in
  forever**. Invisible to RDAP. `tld_words.py` flags ~51 premium-prone TLDs;
  enforce a `--max-renew` budget in the confirm stage (renewal is the real gate).
- **Dot-brand TLDs (closed).** Generic-*looking* strings can be brand-owned and
  closed to the public: `.now`/`.buy`/`.book` (Amazon), `.here`/`.channel`
  (Google), etc. — `shop.now` will never be registrable. `CLOSED_BRAND` lists the
  obvious ones, but the robust fix is an **allowlist from a registrar pricing
  feed** (a TLD is buyable only if a mainstream registrar actually sells it).
- **Credential-gated TLDs.** `.bank`/`.insurance` (charter), `.pharmacy` (NABP),
  `.law` (bar license) show "available" then reject you at verification.
  Excluded by default; opt in with `--include-restricted`.
- **Reserved labels.** Single-/two-char SLDs and `nic`/`whois`/`www` are ICANN-
  reserved even when "available" — filtered in `candidates.py`.
- **ccTLDs.** Most ccTLDs (`.io`/`.ai`/`.me`/`.tv`) lack RDAP; this tool targets
  English-word **gTLDs**. For ccTLD hacks you'd add a WHOIS fallback.

## Possible enhancements

- **Better phrases.** Re-rank by collocation strength (PMI / NLTK
  `likelihood_ratio`) so `silver.lining`-style natural phrases beat incidental
  frequent pairs. Source bigrams from the Google Books Ngram v3 BigQuery public
  table for a larger, register-balanced set.
- **Allowlist from pricing.** Replace the hand-maintained `CLOSED_BRAND` list by
  pulling a registrar's TLD price list and keeping only TLDs actually on sale.
- **ccTLD support.** Add a WHOIS-port-43 fallback + per-registry "no match"
  parsing for the `.io`/`.me`/`.ai` family.

## Data sources & licenses
- TLD list: IANA root zone (`tlds-alpha-by-domain.txt`), public.
- Bigrams: Peter Norvig `count_2w.txt` (MIT). Optionally Google Books Ngram
  v3 (CC BY 3.0).
- Word frequency: `wordfreq` (Apache-2.0 code; CC BY-SA 4.0 data).
- Availability: RDAP via IANA bootstrap (`https://data.iana.org/rdap/dns.json`).
