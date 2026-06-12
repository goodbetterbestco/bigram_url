# bigram_url

Find website addresses that spell a **common two-word English phrase** by
splitting the phrase across the dot — `word1` is the name, `word2` is the TLD —
and that are **available to register right now**.

```
learn . how          free . delivery        music . video
  │      │
  │      └─ word2 = a real TLD that is also an English word (.how, .video, …)
  └──────── word1 = the second-level name
            together, "word1 word2" is a natural English phrase
```

It's a constrained [domain hack](https://en.wikipedia.org/wiki/Domain_hack):
the catch is that **the second word has to be a dictionary word that also
happens to be a top-level domain** (`.now`, `.life`, `.world`, `.how`, `.video`,
`.review`, …).

## Results

A full run found **3,985 available** phrase-domains (snapshot in
[`results/`](results/)), sorted by how common the phrase is. A few from the top:

| domain | phrase frequency |
|---|---:|
| `store.reviews` | 12.2M |
| `free.delivery` | 9.3M |
| `movies.online` | 8.4M |
| `personal.loan` | 2.5M |
| `music.video` | 1.8M |
| `much.fun` | 1.5M |

The genuinely iconic ones (`right.now`, `real.estate`) are long taken or sit on
brand-locked TLDs — what's available is the long tail of real-but-less-obvious
phrases. See [`results/available_domains.csv`](results/available_domains.csv)
for the full ranked list.

## Why this is even tractable

"Search every possible domain" is hopeless. The trick is that **the second word
is drawn from a tiny fixed set**: there are only ~1,440 delegated TLDs in the
entire DNS root, and only **~376 of them are usable English words**. So you
never crawl the web — you intersect:

```
(common two-word phrases)  ∩  (phrases whose 2nd word is one of ~376 TLD-words)
```

From Peter Norvig's 250k most-frequent web bigrams that intersection is only
**~9,500 candidate domains** — small enough to check exhaustively in minutes.

## Quickstart

```bash
git clone https://github.com/goodbetterbestco/bigram_url
cd bigram_url
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. build candidates from the real corpus (auto-downloads Norvig's 5 MB file)
python candidates.py --source norvig --min-zipf-w1 3.0

# 2. check availability via RDAP (polite, host-sharded, resumable)
python scan.py --candidates candidates.jsonl --dns-cull

# results land in results/available_domains.{csv,txt}, sorted by commonality
```

Zero-setup smoke test: `python candidates.py --source demo`.

## How it works

A funnel — cheap filters first, authoritative checks last:

```
 corpus bigrams ─► candidates.py ─► candidates.jsonl
                     • keep pairs whose 2nd word is a TLD-word
                     • strict DNS-label + ICANN-reserved-label + stopword filters
                            │
                            ▼
                         scan.py
   ┌────────────────────────┼─────────────────────────────────┐
   │ 1. DNS cull   │ has NS records? → registered, drop it (cheap, polite)
   │ 2. RDAP scan  │ 404 = available, 200 = taken. Sharded per registry back-end
   │ 3. price (opt)│ registrar API → premium flag + renewal price
   └────────────────────────┴─────────────────────────────────┘
                            ▼
                  results/available_domains.{csv,txt}
```

| file | role |
|---|---|
| `tld_words.py` / `data/word_tlds.tsv` | the ~376 English-word gTLDs + open/premium/restricted/brand flags |
| `build_tld_words.py` | regenerate that list from the live IANA root zone |
| `candidates.py` | bigram corpus → validated `word1.word2` candidates |
| `rdap.py` | polite, host-sharded, resumable RDAP availability checker |
| `registrar_check.py` | optional price/premium confirmation (Name.com adapter) |
| `scan.py` | the end-to-end funnel |

## How long does it take?

A full sweep is **~30–40 minutes**, and the reason is counter-intuitive: the
~376 word-gTLDs don't fan out over hundreds of registries — they map to only
**~54 RDAP back-ends**, and **one operator (Identity Digital) runs most of
them**. So the wall-clock is bounded by politely metering that one busy host
(default ~1 request/sec, backing off on HTTP 429), not by the candidate count.
`rdap.py` shards by back-end and paces each independently; the DNS pre-cull
removes the (large) majority of already-registered names before they ever reach
a registry.

## Important: "available" ≠ "buyable"

RDAP answers *"is it registered?"*, **not** *"can I buy it, and for how much?"*.
The tool flags these; verify before spending money:

- **Registry-premium pricing.** Many one-word second-level names are registry
  "premium" — hundreds to thousands per year, often with the premium renewal
  locked in forever. Invisible to RDAP. The `premium_likely` column flags the
  common cases; `--confirm-price` with a registrar key gets real numbers.
- **Brand / closed TLDs.** Generic-*looking* TLDs can be brand-owned and closed
  to the public (`.now`, `.buy`, `.here`, …) — excluded from candidates.
- **Credential-gated TLDs** (`.bank`, `.law`, `.pharmacy`) show "available" then
  reject you at verification — excluded by default.
- **ccTLDs** (`.io`, `.ai`, `.me`) are out of scope: this targets English-word
  **gTLDs**, and most ccTLDs don't even run RDAP.

## Please be a good citizen

Registry RDAP/WHOIS endpoints are a shared resource with per-IP rate limits.
`rdap.py` is deliberately polite — per-host pacing, honors `Retry-After`,
exponential backoff on 429, a circuit-breaker that stops poking a registry that
keeps refusing, and grouping for shared back-ends (e.g. CentralNic). **Don't
remove those.** For genuinely high-volume availability checking, use a registrar
API or EPP, which is what registries actually want you to use.

## Data sources & licenses

- TLDs: [IANA root zone](https://data.iana.org/TLD/tlds-alpha-by-domain.txt) (public).
- Bigrams: [Peter Norvig's `count_2w.txt`](https://norvig.com/ngrams/) (MIT);
  optionally [Google Books Ngrams](https://books.google.com/ngrams) (CC BY 3.0).
- Word frequency: [`wordfreq`](https://github.com/rspeer/wordfreq) (Apache-2.0 code).
- Availability: RDAP via the [IANA bootstrap](https://data.iana.org/rdap/dns.json).

The code in this repo is released under the MIT License. Third-party data keeps
its own license (above). This is a research/curiosity tool — availability is a
point-in-time snapshot and is not a guarantee you can register or afford any
given name.
