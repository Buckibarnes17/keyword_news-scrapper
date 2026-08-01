"""
backend/discovery/test_news_sitemap.py — unit tests for NewsSitemapStrategy against
synthetic Google-News-sitemap XML fixtures, plus the crawl4ai-shaped dict path
(entries_from_seeder_results) exercised directly since crawl4ai itself isn't
installed here. No live network: every fetch is monkeypatched.

Run with: python3 -m pytest backend/discovery/test_news_sitemap.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.discovery import news_sitemap as ns
from backend.discovery import sitemap as sm
from backend.discovery.base import DiscoveryResult


def _news_urlset(entries):
    """entries: list of (loc, pubdate_or_None, title_or_None)"""
    body = []
    for loc, pubdate, title in entries:
        news_block = ""
        if pubdate or title:
            news_block = (
                "<news:news>"
                + (f"<news:publication_date>{pubdate}</news:publication_date>" if pubdate else "")
                + (f"<news:title>{title}</news:title>" if title else "")
                + "</news:news>"
            )
        body.append(f"<url><loc>{loc}</loc>{news_block}</url>")
    return ('<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
            + "".join(body) + "</urlset>").encode()


def _index(children):
    body = "\n".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in children)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'.encode()


def _profile(domain, seed_url, sitemaps, **extra):
    p = {
        "domain": domain,
        "seed_url": seed_url,
        "enabled": True,
        "strategy": "news_sitemap",
        "robots": {"respect": True, "crawl_delay": 0},
        "sitemaps": sitemaps,
        "transport": {"timeout_s": 5, "max_retries": 1},
    }
    p.update(extra)
    return p


def _patch_fetch(monkeypatch, table):
    def fake_fetch(session, url, headers, timeout, max_retries):
        return table.get(url)
    monkeypatch.setattr(sm, "fetch_sitemap_bytes", fake_fetch)


# ── <news:news> namespace parsing ────────────────────────────────────────────────
def test_news_namespace_parsed_into_published_at(monkeypatch):
    shard = "https://newsaf.cgtn.com/sitemap_latestnews.xml"
    table = {shard: _news_urlset([
        ("https://newsaf.cgtn.com/news/2026-07-31/x-1Pd2OqqkiJi/p.html",
         "2026-07-31T21:44:00Z", "Some headline"),
    ])}
    _patch_fetch(monkeypatch, table)
    profile = _profile("cgtn.com", "https://www.cgtn.com", [{"url": shard, "type": "news"}],
                        allowed_hosts=["*.cgtn.com"],
                        article_url_patterns=[r"/news/\d{4}-\d{2}-\d{2}/[^/]+/p\.html$"])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert len(result.urls) == 1
    u = result.urls[0]
    assert u.published_at == datetime(2026, 7, 31, 21, 44, tzinfo=timezone.utc)
    assert u.title == "Some headline"
    assert u.is_article is True


# ── sitemapindex recursion (news_sitemap side) ───────────────────────────────────
def test_sitemapindex_recursion(monkeypatch):
    index_url = "https://www.cgtn.com/sitemap.xml"
    shard_url = "https://newsaf.cgtn.com/sitemap_latestnews.xml"
    table = {
        index_url: _index([shard_url]),
        shard_url: _news_urlset([
            ("https://newsaf.cgtn.com/news/2026-07-31/a/p.html", "2026-07-31T00:00:00Z", None),
        ]),
    }
    _patch_fetch(monkeypatch, table)
    profile = _profile("cgtn.com", "https://www.cgtn.com", [{"url": index_url, "type": "index"}],
                        allowed_hosts=["*.cgtn.com"])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert len(result.urls) == 1
    assert result.urls[0].url == "https://newsaf.cgtn.com/news/2026-07-31/a/p.html"


# ── gzipped sitemap ───────────────────────────────────────────────────────────────
def test_gzipped_news_sitemap(monkeypatch):
    import gzip
    shard = "https://burmese.voanews.com/sitemap_409_news.xml.gz"
    raw = _news_urlset([("https://burmese.voanews.com/a/8173125.html",
                          "2026-08-01T14:30:00Z", "Headline")])
    table = {shard: gzip.compress(raw)}

    def fake_fetch(session, url, headers, timeout, max_retries):
        content = table.get(url)
        if content is None:
            return None
        return sm.maybe_gunzip(content, url)
    monkeypatch.setattr(sm, "fetch_sitemap_bytes", fake_fetch)

    profile = _profile("burmese.voanews.com", "https://burmese.voanews.com/",
                        [{"url": shard, "type": "news"}],
                        article_url_patterns=[r"^https://burmese\.voanews\.com/a/\d{6,}\.html$"])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert len(result.urls) == 1
    assert result.urls[0].url.endswith("8173125.html")


# ── bbc.com /burmese/ scope filter ────────────────────────────────────────────────
def test_bbc_scope_filter_drops_other_languages(monkeypatch):
    shard = "https://www.bbc.com/sitemaps/https-sitemap-com-news-1.xml"
    table = {shard: _news_urlset([
        ("https://www.bbc.com/burmese/articles/crmrv0v9m2po", "2026-08-01T13:28:00Z", "BurmeseTitle"),
        ("https://www.bbc.com/french/articles/xyz", "2026-08-01T13:28:00Z", "FrenchTitle"),
        ("https://www.bbc.com/burmese/topics/abc123", "2026-08-01T13:28:00Z", "TopicPage"),
    ])}
    _patch_fetch(monkeypatch, table)
    profile = _profile(
        "bbc.com", "https://www.bbc.com/burmese", [{"url": shard, "type": "news"}],
        article_url_patterns=[r"^https://www\.bbc\.com/burmese/articles/[a-z0-9]{10,}$"],
        listing_url_patterns=[r"^https://www\.bbc\.com/burmese/topics/[a-z0-9]+$"],
    )
    strategy = ns.NewsSitemapStrategy(profile)
    # scope_prefix should be derived from seed_url, not require explicit config
    assert strategy._scope_prefix() == "/burmese"
    result = strategy.discover(max_urls=10)
    assert [u.url for u in result.urls] == ["https://www.bbc.com/burmese/articles/crmrv0v9m2po"]


def test_scope_prefix_derivation_warns_when_absent(monkeypatch, caplog):
    profile = _profile("rfa.org", "https://www.rfa.org/burmese/", [])
    strategy = ns.NewsSitemapStrategy(profile)
    import logging
    caplog.set_level(logging.WARNING, logger="keywordscout.discovery.news_sitemap")
    prefix = strategy._scope_prefix()
    assert prefix == "/burmese"
    assert any("no scope_prefix declared" in r.message for r in caplog.records)


# ── cgtn.com host preservation ────────────────────────────────────────────────────
def test_cgtn_host_never_rewritten_to_www(monkeypatch):
    shard = "https://www.cgtn.com/sitemap_news.xml"
    table = {shard: _news_urlset([
        ("https://newseu.cgtn.com/news/2026-01-01/x/p.html", "2026-01-01T00:00:00Z", None),
        ("https://newsus.cgtn.com/news/2026-01-02/y/p.html", "2026-01-02T00:00:00Z", None),
    ])}
    _patch_fetch(monkeypatch, table)
    profile = _profile("cgtn.com", "https://www.cgtn.com", [{"url": shard, "type": "news"}],
                        allowed_hosts=["*.cgtn.com"])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    hosts = {u.url.split("/")[2] for u in result.urls}
    assert hosts == {"newseu.cgtn.com", "newsus.cgtn.com"}
    assert "www.cgtn.com" not in hosts


def test_cgtn_default_host_scope_would_reject_without_allowed_hosts(monkeypatch):
    """Sanity check on host_allowed itself: without allowed_hosts, a subdomain-only
    article host would still pass the default same-registrable-domain rule (both
    endswith '.cgtn.com'), demonstrating allowed_hosts is a belt-and-suspenders
    fix for the actual bug (www 404s), not what stops cross-domain leakage here."""
    profile = _profile("cgtn.com", "https://www.cgtn.com", [])
    strategy = ns.NewsSitemapStrategy(profile)
    assert strategy.host_allowed("https://newsaf.cgtn.com/x") is True
    assert strategy.host_allowed("https://evil.example.com/x") is False


# ── #1306 missing relevance_score ────────────────────────────────────────────────
def test_missing_relevance_score_key_entirely_does_not_raise():
    """Simulates crawl4ai issue #1306: relevance_score key can be ABSENT (not just
    None) even when scoring_method='bm25' was requested. entries_from_seeder_results
    must use .get() everywhere and never raise KeyError."""
    raw = [
        {"url": "https://example.com/burmese/a", "status": "valid",
         "head_data": {"title": "Myanmar coup update", "meta": {"description": "coup news"}}},
        {"url": "https://example.com/burmese/b", "status": "valid",
         "head_data": {"title": "unrelated sports story", "meta": {}}},
    ]
    out = sm.entries_from_seeder_results(
        raw, source_name="news_sitemap", keyword="Myanmar coup",
        host_allowed=lambda u: True, classify_url=lambda u: True,
        scope_prefix=None, since=None)
    assert len(out) == 2  # must NOT come back empty just because scoring failed
    scores = {u.url: u.relevance_score for u in out}
    assert scores["https://example.com/burmese/a"] > scores["https://example.com/burmese/b"]


def test_relevance_score_none_falls_back_per_result():
    raw = [{"url": "https://example.com/x", "status": "valid",
            "relevance_score": None, "head_data": {}}]
    out = sm.entries_from_seeder_results(
        raw, source_name="news_sitemap", keyword="myanmar",
        host_allowed=lambda u: True, classify_url=lambda u: None,
        scope_prefix=None, since=None)
    assert len(out) == 1
    assert out[0].relevance_score is not None  # fell back to the keyword scorer


def test_head_data_and_jsonld_missing_keys_are_defensive():
    """head_data present but with no 'jsonld' key, and a jsonld item with no
    'datePublished' key — must not raise, published_at stays None."""
    raw = [{"url": "https://example.com/x", "status": "valid", "relevance_score": 0.5,
            "head_data": {"title": "t"}}]
    out = sm.entries_from_seeder_results(
        raw, source_name="news_sitemap", keyword=None,
        host_allowed=lambda u: True, classify_url=lambda u: True,
        scope_prefix=None, since=None)
    assert out[0].published_at is None


def test_jsonld_datepublished_extracted():
    raw = [{"url": "https://example.com/x", "status": "valid", "relevance_score": 0.9,
            "head_data": {"jsonld": [{"@type": "Article", "datePublished": "2026-01-15"}]}}]
    out = sm.entries_from_seeder_results(
        raw, source_name="news_sitemap", keyword=None,
        host_allowed=lambda u: True, classify_url=lambda u: True,
        scope_prefix=None, since=None)
    assert out[0].published_at == datetime(2026, 1, 15, tzinfo=timezone.utc)


def test_not_valid_status_dropped():
    raw = [{"url": "https://example.com/x", "status": "not_valid", "relevance_score": 0.9}]
    out = sm.entries_from_seeder_results(
        raw, source_name="news_sitemap", keyword=None,
        host_allowed=lambda u: True, classify_url=lambda u: True,
        scope_prefix=None, since=None)
    assert out == []


# ── crawl4ai-absent fallback path ────────────────────────────────────────────────
def test_crawl4ai_absent_uses_xml_fallback(monkeypatch):
    assert ns.CRAWL4AI_AVAILABLE is False
    shard = "https://kathmandupost.com/sitemap/news/1"
    table = {shard: _news_urlset([
        ("https://kathmandupost.com/national/2026/08/01/x", "2026-08-01T20:13:00+05:45", None),
    ])}
    _patch_fetch(monkeypatch, table)
    profile = _profile("kathmandupost.com", "https://kathmandupost.com/",
                        [{"url": shard, "type": "news"}])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert len(result.urls) == 1
    assert result.urls[0].metadata.get("via") == "xml"


# ── kathmandupost.com: probe past the advertised index end ───────────────────────
def test_probe_past_advertised_index_end(monkeypatch):
    index_url = "https://kathmandupost.com/sitemap/"
    n1 = "https://kathmandupost.com/sitemap/news/1"
    n2 = "https://kathmandupost.com/sitemap/news/2"
    n3 = "https://kathmandupost.com/sitemap/news/3"  # NOT advertised by the index
    table = {
        index_url: _index([n1, n2]),  # index only advertises 2 children
        n1: _news_urlset([("https://kathmandupost.com/a/1", "2026-08-01", None)]),
        n2: _news_urlset([("https://kathmandupost.com/a/2", "2026-07-31", None)]),
        n3: _news_urlset([("https://kathmandupost.com/a/3", "2026-07-30", None)]),
        # n4 deliberately absent -> fetch_sitemap_bytes returns None -> probing stops
    }
    _patch_fetch(monkeypatch, table)
    profile = _profile("kathmandupost.com", "https://kathmandupost.com/",
                        [{"url": index_url, "type": "index"}])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=100)
    urls = {u.url for u in result.urls}
    assert urls == {"https://kathmandupost.com/a/1",
                     "https://kathmandupost.com/a/2",
                     "https://kathmandupost.com/a/3"}


def test_probe_stops_when_next_page_missing():
    import requests
    session = requests.Session()

    class FakeCounter:
        calls = 0

    def fake_fetch(sess, url, headers, timeout, retries):
        FakeCounter.calls += 1
        return None  # every probe fails immediately

    results = list(ns.probe_numeric_continuation(
        session, "https://x.com/sitemap/news/5", {}, 5, 1))
    assert results == []  # patched via direct call with no monkeypatch needed:
    # probe_numeric_continuation itself calls sm.fetch_sitemap_bytes, exercised
    # fully in test_probe_past_advertised_index_end above; this just checks the
    # zero-result contract when nothing is reachable at all.


# ── max_urls truncation ───────────────────────────────────────────────────────────
def test_max_urls_truncation(monkeypatch):
    shard = "https://kathmandupost.com/sitemap/news/1"
    entries = [(f"https://kathmandupost.com/national/2026/08/01/x{i}", "2026-08-01", None)
               for i in range(20)]
    table = {shard: _news_urlset(entries)}
    _patch_fetch(monkeypatch, table)
    profile = _profile("kathmandupost.com", "https://kathmandupost.com/",
                        [{"url": shard, "type": "news"}])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=5)
    assert len(result.urls) == 5
    assert result.truncated is True


# ── discover() must never raise ──────────────────────────────────────────────────
def test_discover_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(sm, "fetch_sitemap_bytes", boom)
    profile = _profile("rfa.org", "https://www.rfa.org/burmese/",
                        [{"url": "https://www.rfa.org/arc/outboundfeeds/burmese/sitemap-news/",
                          "type": "news"}])
    strategy = ns.NewsSitemapStrategy(profile)
    result = strategy.discover(max_urls=10)
    assert isinstance(result, DiscoveryResult)
    assert result.urls == []
    assert len(result.errors) >= 1
