"""
Regenerate data/word_tlds.tsv from the LIVE IANA root zone.

The set of valid second-words changes only when ICANN delegates/retires a TLD
(rarely), so the checked-in data/word_tlds.tsv is fine for most runs. Run this
when you want to refresh it against the authoritative root.

Method (matches the research recommendation):
  1. Fetch IANA's authoritative TLD list (tlds-alpha-by-domain.txt).
  2. Drop IDN/punycode ("XN--") and anything not pure ASCII letters.
  3. Intersect with a *commonness-graded* English dictionary so we keep only
     TLDs that are real, reasonably common words (a TLD like .ngo or .aero is
     delegated but isn't a useful phrase-word). We use wordfreq's zipf score.
  4. Write word<TAB>category(blank)<TAB>premium_flag(0) -- premium/brand/
     restricted flags are applied at load time in tld_words.py from its
     curated CLOSED_BRAND / CREDENTIAL_RESTRICTED sets, so we don't try to
     infer them here.

Usage:
  python build_tld_words.py --min-zipf 3.0 --out data/word_tlds.tsv
"""

import argparse
import sys
import urllib.request

IANA_TLDS = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"


def fetch_tlds():
    req = urllib.request.Request(IANA_TLDS, headers={"User-Agent": "tld-word-builder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    tlds = []
    version = ""
    for line in text.splitlines():
        if line.startswith("#"):
            version = line.strip("# ").strip() or version
            continue
        t = line.strip().lower()
        if t and t.isascii() and t.isalpha() and not t.startswith("xn--"):
            tlds.append(t)
    return tlds, version


def main():
    ap = argparse.ArgumentParser(description="Rebuild word-TLD list from IANA")
    ap.add_argument("--out", default="data/word_tlds.tsv")
    ap.add_argument("--min-zipf", type=float, default=3.0,
                    help="min wordfreq zipf to count a TLD as a 'common word' "
                         "(3.0 ~ word appears >= 1 per million; lower = more)")
    ap.add_argument("--min-len", type=int, default=2,
                    help="ignore 1-char TLDs (none exist) / very short")
    args = ap.parse_args()

    try:
        from wordfreq import zipf_frequency
    except ImportError:
        sys.exit("needs wordfreq:  pip install wordfreq")

    tlds, version = fetch_tlds()
    print(f"IANA root: {version} -- {len(tlds)} ASCII TLDs", file=sys.stderr)

    kept = []
    for t in tlds:
        if len(t) < args.min_len:
            continue
        if zipf_frequency(t, "en") >= args.min_zipf:
            kept.append(t)
    kept.sort()

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(f"# word\tcategory\tpremium_or_restricted  "
                 f"(derived from IANA root {version}, min_zipf={args.min_zipf})\n")
        for t in kept:
            fh.write(f"{t}\t\t0\n")

    print(f"wrote {len(kept)} word-TLDs -> {args.out}", file=sys.stderr)
    print("  e.g.", ", ".join(kept[:30]), file=sys.stderr)


if __name__ == "__main__":
    main()
