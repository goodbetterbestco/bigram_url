"""
Candidate generation: turn a bigram corpus into `word1.word2` domain
candidates where word2 is a TLD-word and "word1 word2" is a real phrase.

Sources (--source):
  demo    : small built-in phrase list -- zero setup, smoke-tests the pipeline.
  norvig  : Peter Norvig's count_2w.txt -- the 250,000 most frequent two-word
            web bigrams with counts (~5MB, MIT). The recommended real source;
            auto-downloaded and cached. https://norvig.com/ngrams/
  tsv     : your own bigram file:  w1<TAB>w2<TAB>count  (e.g. exported from the
            Google Books Ngram v3 BigQuery public table).

Quality knobs:
  --min-count     drop rare bigrams (noise / typos)
  --min-zipf-w1   require word1 to be a reasonably common word (needs wordfreq)
  --include-premium / --include-restricted   widen the TLD set

Local validation we ALWAYS apply (free, removes false candidates before any
network call -- per the registry-gotchas research):
  * strict LDH label rules (length 1-63, [a-z0-9-], no leading/trailing or
    3rd+4th-position double hyphen)
  * ICANN reserved labels dropped: single-char, two-char (reserved by default),
    and nic/whois/www/rdds/example.

Output: candidates.jsonl, highest phrase-frequency first:
  {"domain":"right.now","w1":"right","w2":"now","count":123456,
   "premium":false,"restricted":true,"open":false}
"""

import argparse
import json
import os
import re
import sys
import urllib.request

from tld_words import buyable_tlds

LABEL_RE = re.compile(r"^[a-z0-9-]{1,63}$")
# ICANN Spec-5 reserved second-level labels (always blocked even if "available")
RESERVED_LABELS = {"nic", "whois", "www", "rdds", "example", "rdap"}

# Pure function words that make for junk phrases as the FIRST word
# ("and.services", "than.one"). Deliberately excludes under/over/out/up/down/
# off/back/side etc. since those form catchy compounds (under.world, out.law).
STOPWORDS_W1 = {
    "the", "a", "an", "and", "or", "but", "nor", "of", "to", "in", "on", "at",
    "for", "with", "as", "by", "from", "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "it", "its", "he", "she", "they", "them", "his", "her", "their", "your",
    "you", "we", "our", "us", "i", "my", "me", "who", "whom", "whose",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "do", "does", "did", "has", "have", "had", "not", "no", "than", "then",
    "so", "if", "such", "very", "just", "also", "too", "only", "even",
    "which", "what", "when", "where", "why", "how", "while", "because",
}

NORVIG_URL = "https://norvig.com/ngrams/count_2w.txt"
NORVIG_CACHE = os.path.join(os.path.dirname(__file__), "data", "count_2w.txt")


def valid_first_word(w: str) -> bool:
    """word1 must be a single legal, registrable-looking DNS label."""
    if not w or len(w) > 63 or len(w) < 1:
        return False
    if not LABEL_RE.match(w):
        return False
    if w.startswith("-") or w.endswith("-"):
        return False
    if len(w) >= 4 and w[2] == "-" and w[3] == "-" and not w.startswith("xn--"):
        return False  # R-LDH rule: hyphens in positions 3&4 reserved for punycode
    if not any(c.isalpha() for c in w):
        return False
    if len(w) <= 2:
        return False  # 1- & 2-char SLDs are registry-reserved by default
    if w in RESERVED_LABELS:
        return False
    return True


DEMO_BIGRAMS = [
    ("right", "now", 9_000_000), ("live", "now", 1_200_000),
    ("buy", "now", 3_000_000), ("good", "life", 1_500_000),
    ("night", "life", 900_000), ("real", "estate", 5_000_000),
    ("true", "love", 1_300_000), ("home", "run", 700_000),
    ("night", "club", 1_000_000), ("book", "club", 600_000),
    ("dream", "team", 800_000), ("talk", "show", 900_000),
    ("comfort", "zone", 1_400_000), ("war", "zone", 700_000),
    ("open", "house", 900_000), ("under", "world", 400_000),
    ("fire", "works", 1_200_000), ("net", "works", 900_000),
    ("home", "page", 800_000), ("web", "site", 1_500_000),
    ("birth", "day", 5_000_000), ("pay", "day", 900_000),
    ("happy", "hour", 0), ("school", "bus", 0),  # w2 not a TLD -> dropped
]


def from_demo():
    for w1, w2, c in DEMO_BIGRAMS:
        yield w1.lower(), w2.lower(), c


def from_norvig(path=NORVIG_CACHE):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"downloading Norvig bigrams -> {path} ...", file=sys.stderr)
        req = urllib.request.Request(NORVIG_URL,
                                     headers={"User-Agent": "phrase-domain/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as out:
            out.write(r.read())
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # format: "word1 word2<TAB>count"
            left, _, count = line.rstrip("\n").partition("\t")
            toks = left.split()
            if len(toks) != 2:
                continue
            try:
                c = int(count)
            except ValueError:
                continue
            yield toks[0].lower(), toks[1].lower(), c


def from_tsv(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                yield parts[0].lower(), parts[1].lower(), int(parts[2])
            except ValueError:
                continue


def generate(source_iter, tld_set, min_count=0, min_zipf_w1=0.0,
             drop_stopwords=True):
    zipf = None
    if min_zipf_w1 > 0:
        try:
            from wordfreq import zipf_frequency
            zipf = zipf_frequency
        except ImportError:
            print("warning: wordfreq not installed; --min-zipf-w1 ignored",
                  file=sys.stderr)

    seen = set()
    for w1, w2, count in source_iter:
        if count < min_count:
            continue
        e = tld_set.get(w2)            # second word must be a buyable TLD-word
        if e is None:
            continue
        if not valid_first_word(w1):
            continue
        if drop_stopwords and w1 in STOPWORDS_W1:
            continue
        if zipf and zipf(w1, "en") < min_zipf_w1:
            continue
        domain = f"{w1}.{w2}"
        if domain in seen:
            continue
        seen.add(domain)
        yield {
            "domain": domain, "w1": w1, "w2": w2, "count": count,
            "premium": e.premium, "restricted": e.restricted, "open": e.open,
        }


def main():
    ap = argparse.ArgumentParser(description="Generate phrase-domain candidates")
    ap.add_argument("--source", choices=["demo", "norvig", "tsv"], default="demo")
    ap.add_argument("--tsv", help="bigram TSV path (for --source tsv)")
    ap.add_argument("--out", default="candidates.jsonl")
    ap.add_argument("--min-count", type=int, default=0)
    ap.add_argument("--min-zipf-w1", type=float, default=0.0,
                    help="min wordfreq zipf for word1 (e.g. 3.0 = fairly common)")
    ap.add_argument("--include-premium", action="store_true",
                    help="(on by default; premium are always generated+flagged)")
    ap.add_argument("--include-restricted", action="store_true",
                    help="also include credential-gated TLDs (.bank/.law/...)")
    ap.add_argument("--keep-stopwords", action="store_true",
                    help="keep function-word first-words (and./the./than.)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    tld_set = buyable_tlds(include_premium=True,
                           include_restricted=args.include_restricted)

    if args.source == "norvig":
        src = from_norvig()
    elif args.source == "tsv":
        if not args.tsv:
            ap.error("--source tsv requires --tsv PATH")
        src = from_tsv(args.tsv)
    else:
        src = from_demo()

    rows = list(generate(src, tld_set, args.min_count, args.min_zipf_w1,
                         drop_stopwords=not args.keep_stopwords))
    rows.sort(key=lambda r: r["count"], reverse=True)
    if args.limit:
        rows = rows[: args.limit]

    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    n_prem = sum(1 for r in rows if r["premium"])
    n_tld = len({r["w2"] for r in rows})
    print(f"wrote {len(rows)} candidates across {n_tld} TLDs -> {args.out} "
          f"({n_prem} flagged premium)")
    for r in rows[:15]:
        flags = ",".join(f for f, on in
                         [("premium", r["premium"]), ("restricted", r["restricted"])]
                         if on)
        tag = f"  [{flags}]" if flags else ""
        print(f"  {r['domain']:<26} count={r['count']:>12}{tag}")


if __name__ == "__main__":
    main()
