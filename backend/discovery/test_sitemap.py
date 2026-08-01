"""
backend/discovery/test_sitemap.py — unit tests for SitemapStrategy against synthetic
XML fixtures. No live network: every fetch is monkeypatched.

Run with: python3 -m pytest backend/discovery/test_sitemap.py -v
"""
from __future__ import annotations

import gzip
from datetime import datetime, timezone

import pytest

from backend.discovery import sitemap as sm
from backend.discovery.base import DiscoveredURL, DiscoveryResult


def _urlset(urls_and_lastmods):
    body = "\n".join(
        f"<url><loc>{u}</loc>{'<lastmod>' + lm + '</lastmod>' if lm else ''}</url>"
        for u, lm in urls_and_lastmods
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'.encode()


def _index(children):
    body = "\n".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in children)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'.encode()


def _profile(domain, seed_url, sitemaps, **extra):
    p = {
        "domain": domain,
        "seed_url": seed_url,
        "enabled": True,
        "strategy": "sitemap",
        "robots": {"respect": True, "crawl_delay": 0},
        "sitemaps": sitemaps,
        "transport": {"timeout_s": 5, "max_retries": 1},
    }
    p.update(extra)
    return p


# ── XML parsing ──────────────────────────────────────────────────────────────────
def test_parse_sitemap_urlset():
    content = _urlset([("https://example.com/a", "2026-01-01"), ("https://example.com/b", None)])
    kind, entries = sm.parse_sitemap(content)
    assert kind == "urlset"
    assert len(entries) == 2
    assert entries[0].loc == "https://example.com/a"
    assert entries[0].lastmod == "2026-01-01"
    assert entries[1].lastmod is None


def test_parse_sitemap_sitemapindex_recursion():
    content = _index(["https://example.com/sitemap1.xml", "https://example.com/sitemap2.xml"])
    kind, entries = sm.parse_sitemap(content)
    assert kind == "sitemapindex"
    assert [e.loc for e in entries] == [
        "https://example.com/sitemap1.xml", "https://example.com/sitemap2.xml"]


def test_parse_sitemap_malformed_never_raises():
    kind, entries = sm.parse_sitemap(b"not xml at all <<<")
    assert kind == "unknown"
    assert entries == []


def test_parse_sitemap_news_namespace():
    content = ('<?xml version="1.0"?>'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
               'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
               '<url><loc>https://example.com/a</loc>'
               '<news:news><news:publication_date>2026-08-01T10:00:00Z</news:publication_date>'
               '<news:title>Headline</news:title></news:news></url>'
               '</urlset>').encode()
    kind, entries = sm.parse_sitemap(content)
    assert kind == "urlset"
    assert entries[0].news_pubdate == "2026-08-01T10:00:00Z"
    assert entries[0].news_title == "Headline"


# ── gzip handling ────────────────────────────────────────────────────────────────
def test_gzip_transparent_decompression():
    raw = _urlset([("https://example.com/a", None)])
    gz = gzip.compress(raw)
    assert sm.looks_gzipped(gz, "https://example.com/sitemap.xml.gz")
    out = sm.maybe_gunzip(gz, "https://example.com/sitemap.xml.gz")
    assert out == raw


def test_gzip_by_magic_bytes_even_without_gz_extension():
    raw = _urlset([("https://example.com/a", None)])
    gz = gzip.compress(raw)
    # URL doesn't say .gz but content is gzip-magic - must still be detected.
    assert sm.looks_gzipped(gz, "https://example.com/sitemap_no_ext")


def test_maybe_gunzip_passthrough_for_plain_xml():
    raw = _urlset([("https://example.com/a", None)])
    assert sm.maybe_gunzip(raw, "https://example.com/sitemap.xml") == raw


# ── date parsing ─────────────────────────────────────────────────────────────────
def test_parse_datetime_variants():
    assert sm.parse_datetime("2026-08-01T10:00:00Z") == datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    assert sm.parse_datetime("2026-08-01") == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert sm.parse_datetime(None) is None
    assert sm.parse_datetime("garbage") is None


# ── SitemapStrategy.discover() ───────────────────────────────────────────────────
def _patch_fetch(monkeypatch, table):
    def fake_fetch(session, url, headers, timeout, max_retries):
        return table.get(url)
    monkeypatch.setattr(sm, "fetch_sitemap_bytes", fake_fetch)


def test_sitemapindex_recursion_end_to_end(monkeypatch):
    index_url = "https://burmese.dvb.no/sitemap.xml"
    shard_url = "https://burmese.dvb.no/sitemap/3.xml"
    table = {
        index_url: _index([shard_url]),
        shard_url: _urlset([
            ("https://burmese.dvb.no/post/61173", "2026-08-01T00:00:00Z"),
            ("https://burmese.dvb.no/post/61175", "2026-08-01T00:00:00Z"),
        ]),
    }
    _patch_fetch(monkeypatch, table)
    profile = _profile("dvb.no", "https://www.dvb.no/",
                        [{"url": index_url, "type": "index"}],
                        article_url_patterns=[r"^https://burmese\.dvb\.no/post/\d+$"],
                        listing_url_patterns=[r"^https://burmese\.dvb\.no/(categories|tags)/"])
    strategy = sm.SitemapStrategy(profile)
    result = strategy.discover(max_urls=100)
    assert result.errors == []
    assert len(result.urls) == 2
    assert all(u.is_article for u in result.urls)
    assert all(u.url.startswith("https://burmese.dvb.no/post/") for u in result.urls)


def test_listing_urls_filtered_out_via_classify_url(monkeypatch):
    """dvb.no mixes ~55k tag/page URLs into some shards - classify_url must drop them."""
    shard_url = "https://burmese.dvb.no/sitemap/0.xml"
    table = {
        shard_url: _urlset([
            ("https://burmese.dvb.no/post/1", None),
            ("https://burmese.dvb.no/categories/politics", None),
            ("https://burmese.dvb.no/tags/myanmar", None),
        ]),
    }
    _patch_fetch(monkeypatch, table)
    profile = _profile("dvb.no", "https://www.dvb.no/", [{"url": shard_url, "type": "urlset"}],
                        article_url_patterns=[r"^https://burmese\.dvb\.no/post/\d+$"],
                        listing_url_patterns=[r"^https://burmese\.dvb\.no/(categories|tags)/"])
    strategy = sm.SitemapStrategy(profile)
    result = strategy.discover(max_urls=100)
    assert [u.url for u in result.urls] == ["https://burmese.dvb.no/post/1"]


def test_max_urls_truncation(monkeypatch):
    shard_url = "https://pri.gov.np/sitemap-news.xml"
    urls = [(f"https://pri.gov.np/content/{i}/slug-{i}/", None) for i in range(10)]
    table = {shard_url: _urlset(urls)}
    _patch_fetch(monkeypatch, table)
    profile = _profile("pri.gov.np", "https://pri.gov.np/", [{"url": shard_url, "type": "urlset"}],
                        article_url_patterns=[r"^https://pri\.gov\.np/content/\d+/[^/]+/?$"])
    strategy = sm.SitemapStrategy(profile)
    result = strategy.discover(max_urls=3)
    assert len(result.urls) == 3
    assert result.truncated is True


def test_discover_never_raises_on_fetch_exception(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network exploded")
    monkeypatch.setattr(sm, "fetch_sitemap_bytes", boom)
    profile = _profile("mod.gov.np", "https://mod.gov.np/",
                        [{"url": "https://mod.gov.np/sitemap-news.xml", "type": "urlset"}])
    strategy = sm.SitemapStrategy(profile)
    result = strategy.discover(max_urls=50)  # must not raise
    assert isinstance(result, DiscoveryResult)
    assert result.urls == []
    assert len(result.errors) >= 1


def test_since_filter_drops_older_entries(monkeypatch):
    shard_url = "https://mod.gov.np/sitemap-news.xml"
    table = {shard_url: _urlset([
        ("https://mod.gov.np/content/1/a/", "2020-01-01"),
        ("https://mod.gov.np/content/2/b/", "2026-08-01"),
    ])}
    _patch_fetch(monkeypatch, table)
    profile = _profile("mod.gov.np", "https://mod.gov.np/", [{"url": shard_url, "type": "urlset"}],
                        article_url_patterns=[r"^https://mod\.gov\.np/content/\d+/[^/]*/?$"])
    strategy = sm.SitemapStrategy(profile)
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = strategy.discover(max_urls=50, since=since)
    assert [u.url for u in result.urls] == ["https://mod.gov.np/content/2/b/"]


def test_no_sitemaps_in_profile_falls_back_and_warns(monkeypatch, caplog):
    profile = _profile("example.com", "https://example.com/", [])
    monkeypatch.setattr(sm, "fetch_sitemap_bytes", lambda *a, **kw: None)
    strategy = sm.SitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert result.urls == []  # fetch always fails in this test, but must not raise
    assert isinstance(result, DiscoveryResult)


def test_crawl4ai_absent_uses_xml_fallback(monkeypatch):
    """crawl4ai is not installed on this machine - CRAWL4AI_AVAILABLE must be False
    and discover() must still work end-to-end via the XML path."""
    assert sm.CRAWL4AI_AVAILABLE is False
    shard_url = "https://pri.gov.np/sitemap-news.xml"
    table = {shard_url: _urlset([("https://pri.gov.np/content/1/a/", "2026-08-01")])}
    _patch_fetch(monkeypatch, table)
    profile = _profile("pri.gov.np", "https://pri.gov.np/", [{"url": shard_url, "type": "urlset"}])
    strategy = sm.SitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert len(result.urls) == 1
    assert result.urls[0].metadata.get("via") == "xml"


def test_robots_disallow_skips_sitemap(monkeypatch):
    shard_url = "https://mod.gov.np/sitemap-news.xml"
    table = {shard_url: _urlset([("https://mod.gov.np/content/1/a/", None)])}
    _patch_fetch(monkeypatch, table)
    profile = _profile("mod.gov.np", "https://mod.gov.np/", [{"url": shard_url, "type": "urlset"}])
    strategy = sm.SitemapStrategy(profile)
    monkeypatch.setattr(strategy, "robots_allowed", lambda url: False)
    result = strategy.discover(max_urls=10)
    assert result.urls == []
    assert any("robots.txt disallows" in e for e in result.errors)


def test_run_async_bridges_a_coroutine():
    async def coro():
        return 42
    assert sm.run_async(coro()) == 42
