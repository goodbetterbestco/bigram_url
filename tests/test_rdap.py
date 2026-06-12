"""Tests for RDAP helpers -- especially the Retry-After parser, which is reached
only on a 429 and must never raise, and pacing/resume logic."""

import json

import rdap


def test_parse_retry_after_integer_seconds():
    assert rdap._parse_retry_after("120") == 120.0


def test_parse_retry_after_float_seconds():
    # a misbehaving registry can send a non-integer -- must not crash
    assert rdap._parse_retry_after("120.5") == 120.5


def test_parse_retry_after_garbage_returns_none():
    # the whole bug: these used to raise ValueError and kill the scan
    for bad in ["", "5; comment", "garbage", "soon"]:
        assert rdap._parse_retry_after(bad) is None


def test_parse_retry_after_negative_clamped():
    assert rdap._parse_retry_after("-30") == 0.0


def test_parse_retry_after_http_date_is_nonnegative_or_none():
    val = rdap._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert val is None or val >= 0.0


def test_pacing_key_groups_centralnic():
    assert rdap.pacing_key("https://rdap.nic.one/") == "centralnic-shared"
    assert rdap.pacing_key("https://rdap.nic.health/") == "centralnic-shared"
    assert rdap.pacing_key("https://rdap.centralnic.com/") == "centralnic-shared"


def test_pacing_key_keeps_other_hosts_distinct():
    assert rdap.pacing_key("https://rdap.identitydigital.services") == \
        "rdap.identitydigital.services"


def test_load_done_only_counts_definitive(tmp_path):
    p = tmp_path / "results.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"domain": "a.one", "status": "available"},
        {"domain": "b.one", "status": "taken"},
        {"domain": "c.one", "status": "unknown"},   # retryable, not "done"
        {"domain": "d.one", "status": "no_rdap_server"},
    ]) + "\n")
    done = rdap.load_done(str(p))
    assert done == {"a.one": "available", "b.one": "taken"}
    assert "c.one" not in done   # unknown is retried on resume
