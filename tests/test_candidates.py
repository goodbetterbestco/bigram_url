"""Tests for candidate generation: label validation and filtering."""

import candidates
from tld_words import buyable_tlds


def test_valid_first_word_accepts_normal_words():
    for w in ["store", "right", "free", "mortgage", "good-faith"]:
        assert candidates.valid_first_word(w), w


def test_valid_first_word_rejects_short_labels():
    # single- and two-char SLDs are registry-reserved by default
    assert not candidates.valid_first_word("a")
    assert not candidates.valid_first_word("ab")


def test_valid_first_word_rejects_hyphen_edges():
    assert not candidates.valid_first_word("-lead")
    assert not candidates.valid_first_word("trail-")


def test_valid_first_word_rejects_r_ldh_double_hyphen():
    # hyphens in positions 3 and 4 are reserved for punycode (xn--)
    assert not candidates.valid_first_word("ab--cd")


def test_valid_first_word_rejects_reserved_labels():
    for w in candidates.RESERVED_LABELS:
        assert not candidates.valid_first_word(w), w


def test_valid_first_word_requires_alpha():
    assert not candidates.valid_first_word("1234")


def test_generate_keeps_only_tld_word_pairs():
    tlds = buyable_tlds()
    src = [
        ("free", "delivery", 100),   # .delivery is a word-TLD -> kept
        ("happy", "hour", 100),      # .hour is not a TLD -> dropped
    ]
    out = [r["domain"] for r in candidates.generate(src, tlds)]
    assert "free.delivery" in out
    assert "happy.hour" not in out


def test_generate_drops_stopword_first_words_by_default():
    tlds = buyable_tlds()
    src = [("and", "services", 100), ("real", "estate", 100)]
    out = [r["domain"] for r in candidates.generate(src, tlds)]
    assert "and.services" not in out
    assert "real.estate" in out


def test_generate_respects_min_count():
    tlds = buyable_tlds()
    src = [("free", "delivery", 5)]
    assert list(candidates.generate(src, tlds, min_count=10)) == []
    assert len(list(candidates.generate(src, tlds, min_count=1))) == 1
