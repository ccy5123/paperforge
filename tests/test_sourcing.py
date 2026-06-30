"""Tests for the habanero-backed sourcing wrapper.

The wrapper is the single network boundary. It must return a SourcedEntry whose
raw_bibtex is byte-identical to what habanero returned (I8 provenance), and None
on the unresolved path -- without fabricating anything.
"""
from datetime import datetime

import httpx2
import pytest

import paperforge.sourcing as sourcing
from paperforge.sourcing import SourcedEntry, fetch_canonical_bibtex

BODY = (
    "@article{key,\n"
    "  title = {Environmental Science &amp; Technology},\n"
    "  author = {Doe, Jane},\n"
    "  year = {2021}\n"
    "}\n"
)


def test_returns_sourced_entry_with_byte_identical_raw(monkeypatch):
    seen = {}

    def fake_cn(ids, format="bibtex", **kw):
        seen["ids"] = ids
        seen["format"] = format
        seen["kw"] = kw
        return BODY

    monkeypatch.setattr(sourcing.cn, "content_negotiation", fake_cn)
    entry = fetch_canonical_bibtex("10.1021/es0xxxxx", mailto="me@example.org")

    assert isinstance(entry, SourcedEntry)
    assert entry.raw_bibtex == BODY                 # I8: byte-identical, untouched
    assert entry.doi == "10.1021/es0xxxxx"
    assert isinstance(entry.retrieved_at, datetime)
    assert seen["ids"] == "10.1021/es0xxxxx"
    assert seen["format"] == "bibtex"


def test_mailto_is_passed_through_for_etiquette(monkeypatch):
    seen = {}
    monkeypatch.setattr(sourcing.cn, "content_negotiation",
                        lambda ids, format="bibtex", **kw: seen.update(kw) or BODY)
    fetch_canonical_bibtex("10.1/x", mailto="polite@example.org")
    # the mailto reaches habanero (as a polite-pool query param)
    assert "polite@example.org" in repr(seen)


def test_unresolved_doi_returns_none(monkeypatch):
    def boom(ids, format="bibtex", **kw):
        raise httpx2.HTTPStatusError("404", request=None, response=None)

    monkeypatch.setattr(sourcing.cn, "content_negotiation", boom)
    assert fetch_canonical_bibtex("10.1/missing", mailto="m@example.org") is None


def test_non_bibtex_body_returns_none(monkeypatch):
    monkeypatch.setattr(sourcing.cn, "content_negotiation",
                        lambda ids, format="bibtex", **kw: "<html>landing</html>")
    assert fetch_canonical_bibtex("10.1/html", mailto="m@example.org") is None


def test_empty_doi_returns_none_without_network(monkeypatch):
    called = {"n": 0}

    def tick(*a, **k):
        called["n"] += 1
        return BODY

    monkeypatch.setattr(sourcing.cn, "content_negotiation", tick)
    assert fetch_canonical_bibtex("", mailto="m@example.org") is None
    assert called["n"] == 0                         # never hits the network
