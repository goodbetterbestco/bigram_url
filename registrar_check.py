"""
Buyability + price confirmation -- the stage RDAP cannot do.

Hard-won lesson from the research: RDAP/WHOIS answer "is it registered?", NOT
"can I buy it, and for how much?". A name can RDAP-404 (look available) yet be:
  - registry-reserved / name-collision-blocked (unbuyable),
  - a registry PREMIUM name ($100s-$1000s/yr, with premium *renewals*),
  - under a dot-brand TLD nobody can register in.

So the only safe "BUYABLE" signal is a registrar/reseller availability API that
returns: available bool + premium flag + create price + RENEWAL price. This
module defines that interface and a Name.com adapter (good free batch limits:
50 domains/call, 20 req/s, 3000/hr). Plug in your own keys via env vars.

Without credentials, scan.py still works -- it just reports RDAP "available
(unconfirmed)" and labels likely-premium TLDs so you know which to price-check.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class PriceResult:
    domain: str
    purchasable: bool
    premium: bool
    create_price: float | None
    renew_price: float | None
    raw: dict


class NameComChecker:
    """Name.com Core API adapter. Set NAMECOM_USER / NAMECOM_TOKEN.

    Free, batchable (up to 50 domains per call), 3000 req/hr. Use api.name.com
    (prod) or api.dev.name.com (test, append '-test' to username).
    """

    BATCH = 50

    def __init__(self, user=None, token=None, base="https://api.name.com"):
        self.user = user or os.environ.get("NAMECOM_USER")
        self.token = token or os.environ.get("NAMECOM_TOKEN")
        self.base = base
        if not (self.user and self.token):
            raise RuntimeError("set NAMECOM_USER and NAMECOM_TOKEN env vars")

    def _auth(self):
        raw = f"{self.user}:{self.token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def check(self, domains):
        """domains: list[str] -> list[PriceResult]. Batches of 50.

        A failed batch is logged and skipped (its names simply don't appear in
        the result) rather than crashing the whole price check.
        """
        out = []
        for i in range(0, len(domains), self.BATCH):
            chunk = domains[i: i + self.BATCH]
            body = json.dumps({"domainNames": chunk}).encode()
            req = urllib.request.Request(
                f"{self.base}/core/v1/domains:checkAvailability",
                data=body, method="POST",
                headers={"Authorization": self._auth(),
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                print(f"warning: price-check batch {i // self.BATCH} failed "
                      f"({e}); skipping {len(chunk)} names", file=sys.stderr)
                continue
            for res in data.get("results", []):
                out.append(PriceResult(
                    domain=res.get("domainName", ""),
                    purchasable=res.get("purchasable", False),
                    premium=res.get("premium", False),
                    create_price=res.get("purchasePrice"),
                    renew_price=res.get("renewalPrice"),
                    raw=res,
                ))
        return out


def get_checker(provider="namecom"):
    """Factory; extend with GoDaddy/Cloudflare/DNSimple adapters as needed."""
    if provider == "namecom":
        return NameComChecker()
    raise ValueError(f"unknown provider {provider!r} "
                     "(implement an adapter in registrar_check.py)")


def within_budget(p: PriceResult, max_create=None, max_renew=None) -> bool:
    """Renewal is the real gate -- premium renewals lock in forever."""
    if not p.purchasable:
        return False
    if max_renew is not None:
        if p.renew_price is None or p.renew_price > max_renew:
            return False
    if max_create is not None:
        if p.create_price is None or p.create_price > max_create:
            return False
    return True
