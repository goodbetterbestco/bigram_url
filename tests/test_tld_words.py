"""Tests for the word-TLD set and its open/premium/restricted flags."""

from tld_words import WORD_TLDS, buyable_tlds, CLOSED_BRAND, CREDENTIAL_RESTRICTED


def test_closed_brands_never_buyable():
    b = buyable_tlds(include_premium=True, include_restricted=True)
    for word in CLOSED_BRAND:
        assert word not in b, f"{word} is a dot-brand and must not be buyable"


def test_credential_restricted_excluded_by_default():
    b = buyable_tlds()
    for word in CREDENTIAL_RESTRICTED:
        if word in WORD_TLDS:
            assert word not in b, f"{word} is credential-gated"


def test_credential_restricted_opt_in():
    b = buyable_tlds(include_restricted=True)
    # at least one known credential TLD that's in the data should appear
    present = [w for w in CREDENTIAL_RESTRICTED if w in WORD_TLDS]
    assert present, "expected some credential-restricted TLDs in the data"
    assert all(w in b for w in present)


def test_premium_included_by_default_but_droppable():
    with_premium = buyable_tlds(include_premium=True)
    without_premium = buyable_tlds(include_premium=False)
    assert len(without_premium) < len(with_premium)


def test_known_words_present():
    for w in ["delivery", "online", "reviews", "loan"]:
        assert w in WORD_TLDS
