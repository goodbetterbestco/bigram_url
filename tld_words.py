"""
TLD words = the universe of valid "second words" for a phrase domain.

A candidate domain is  word1.word2  where:
  - word2 is the TLD (the part after the final dot), and
  - word2 is itself a common English word, and
  - "word1 word2" is a natural two-word English phrase.

So this file defines the *closed* set of words allowed to be the second word.
There are only ~1,440 delegated TLDs total and only a few hundred are real
English words -- that small ceiling is what makes the whole search tractable
(see README, "Why this is fast").

The base list lives in data/word_tlds.tsv, derived from the live IANA root
zone (regenerate with build_tld_words.py). On top of it we apply flags so the
scanner never reports a domain that is technically unregistered but not
realistically *buyable*:

  open=False  : delegated but NOT open to public registration -- dot-brand
                (Spec 13) TLDs like .now/.buy/.next/.book run by one company.
                'shop.now' may even RDAP-404, yet nobody can buy it.
  restricted  : open, but registration requires credentials/eligibility
                (.bank/.insurance fTLD, .pharmacy NABP, .law bar license).
                Shows as "available" then fails at post-purchase verification.
  premium     : second-level dictionary words are frequently REGISTRY-premium
                priced here (hundreds-thousands/yr, with premium *renewals*).
                RDAP cannot see price -- confirm with a registrar price API
                (see registrar_check.py) before trusting "available + cheap".

These flag sets come from the research pass (ICANN Spec 5/13, fTLD/NABP,
Amazon/Google brand portfolios). They are intentionally conservative.
"""

import os
from dataclasses import dataclass

_DATA = os.path.join(os.path.dirname(__file__), "data", "word_tlds.tsv")

# Delegated but CLOSED to the public (dot-brand / never opened). A phrase like
# right.now / click.here looks perfect but cannot be registered by anyone.
# NOTE: this hand-list is necessarily incomplete -- there are ~450 dot-brand
# TLDs and ICANN's Spec-13 list churns. The robust production fix is an
# *allowlist* derived from a registrar pricing feed (a TLD is buyable only if a
# mainstream registrar actually sells it); see README "Brand-TLD false positives".
CLOSED_BRAND = {
    # Amazon Registry brands (delegated, never opened to the public)
    "now", "buy", "book", "song", "tunes", "smile", "wow", "joy", "deal",
    "prime", "save", "fast", "secure", "spot", "read", "room", "call",
    "circle", "zero", "got", "like", "pay", "hot", "safe", "silver",
    # Google (Charleston Road Registry) brands / verb-restricted
    "new", "here", "channel", "dot", "boo", "eat", "fly", "meme", "prod",
    "search", "play", "ads", "drive", "gle", "goog", "rsvp",
    # Other dot-brands that look generic
    "next",   # Next plc (UK retailer)
}

# Open, but registration is credential-gated -- an ordinary buyer is rejected
# at verification. Excluded by default; include with --include-restricted.
CREDENTIAL_RESTRICTED = {
    "bank", "insurance",   # fTLD: verified chartered/licensed institution
    "pharmacy",            # NABP accreditation
    "law", "abogado",      # bar license
    "cpa",                 # licensed CPA
    "music",               # verified music-industry registrant
    "jobs", "travel",      # sponsored/charter
}


@dataclass(frozen=True)
class TldWord:
    word: str
    open: bool = True
    premium: bool = False
    restricted: bool = False
    category: str = ""

    @property
    def tld(self):
        return self.word


def _load(path=_DATA):
    entries = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            word = parts[0].strip().lower()
            category = parts[1] if len(parts) > 1 else ""
            prem_flag = parts[2].strip() == "1" if len(parts) > 2 else False
            is_closed = word in CLOSED_BRAND
            is_restricted = word in CREDENTIAL_RESTRICTED
            # registry-premium if the source flagged it and it isn't already
            # explained by being closed/credential-restricted
            is_premium = prem_flag and not is_closed and not is_restricted
            entries[word] = TldWord(
                word=word,
                open=not is_closed,
                premium=is_premium,
                restricted=is_restricted,
                category=category,
            )
    return entries


#: dict[word] -> TldWord  (the full delegated word-TLD universe)
WORD_TLDS = _load()


def buyable_tlds(include_premium=True, include_restricted=False):
    """The set of second-words we'll actually treat as candidate-buyable.

    By default: open TLDs only, premium kept but flagged, credential-gated and
    dot-brand TLDs excluded (you cannot realistically buy those).
    """
    out = {}
    for word, e in WORD_TLDS.items():
        if not e.open:
            continue                       # dot-brand: never buyable
        if e.restricted and not include_restricted:
            continue
        if e.premium and not include_premium:
            continue
        out[word] = e
    return out


if __name__ == "__main__":
    total = len(WORD_TLDS)
    buyable = len(buyable_tlds())
    premium = sum(1 for e in WORD_TLDS.values() if e.premium)
    closed = sum(1 for e in WORD_TLDS.values() if not e.open)
    restricted = sum(1 for e in WORD_TLDS.values() if e.restricted)
    print(f"{total} delegated word-TLDs")
    print(f"  {buyable} buyable-by-default (open, non-restricted)")
    print(f"  {premium} registry-premium-likely (flagged, still generated)")
    print(f"  {closed} dot-brand / closed   {restricted} credential-restricted")
    print("  closed examples:", ", ".join(sorted(CLOSED_BRAND)))
