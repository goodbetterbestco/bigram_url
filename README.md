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
only **~9,700 candidate domains across ~315 TLDs** (after dropping function-word
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
   │ 2. RDAP scan      │ 404=available, 200=taken. Sharded per-TLD. PRIMARY.
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

In principle, RDAP work **self-shards across ~315 independent registry servers**
(each TLD has its own RDAP endpoint), so the theoretical floor is the *busiest
single TLD* — `.one` with 307 names at ~2 qps ≈ **~2.5 min**.

In practice it's slower, and measuring it honestly matters. Empirically (this
machine, polite throttle of ~2 qps/host, concurrency 40):

| working set | regime | sustained throughput |
|---|---|---|
| 400 most-frequent phrases | clustered into a few popular TLDs | ~4 dom/s |
| ~470 random phrases | spread across ~300 TLDs | **~2.5 dom/s, decelerating** |

The diverse sample is *slower*, not faster — because real registries enforce
their own rate limits, returning HTTP 429 and forcing exponential backoff, and
the tail of the run concentrates on the slowest / most-throttled registry
endpoints (observed >5 s/domain near the end). So the busiest-TLD floor is not
achievable in practice.

**Realistic full sweep of ~9,700 candidates: roughly 30–90 minutes** of polite
scanning, dominated by per-registry rate limits and the slow tail, not by CPU or
the candidate count. Levers that help:
- **DNS pre-cull first** (`--dns-cull`, thousands/sec) removes the large fraction
  of already-taken common phrases before they ever reach RDAP.
- **Tune throttling per registry** rather than one global rate; the fast
  registries can take 5+ qps while a few slow ones gate everything.
- A **registrar batch API** (Name.com: 50 names/call, 3000 calls/hr ⇒ ~150k
  names/hr) is both faster *and* returns price/premium — at the cost of an
  account and a monthly quota.

Bottom line: this is a **minutes-to-an-hour** job, not a multi-day crawl,
because the search space is only ~10k names. The wall-clock is a politeness/
rate-limit budget, not an algorithmic one.

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
