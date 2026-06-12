# Results (complete)

Common two-word English phrases registrable as `word1.word2` (word2 = an
English-word gTLD), checked live via RDAP and sorted by phrase commonality.

| metric | count |
|---|---|
| candidates (phrase x word-TLD) | 9466 |
| DNS pre-culled as registered | ~4,955 |
| **available (RDAP 404)** | **3985** |
| taken (RDAP 200) | 650 |
| unresolved | 0 |

`available_domains.csv` is sorted by `phrase_count` (web-bigram frequency)
descending, with a stable `rank` column. Availability = registrable per RDAP;
price/premium not verified (see `premium_likely`).
