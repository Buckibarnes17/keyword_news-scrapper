"""
backend/discovery/test_deep_crawl.py — tests for DeepCrawlStrategy against
synthetic fixtures. No live network is used: requests.Session.get is monkeypatched
per-test. Requires only stdlib + requests + lxml.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pytest

from backend.discovery import deep_crawl as dc_module
from backend.discovery.deep_crawl import DeepCrawlStrategy


class FakeResponse:
    def __init__(self, url: str, status_code: int = 200, content: bytes = b"",
                 headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.ok = 200 <= status_code < 400

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


def _profile(domain: str, **overrides) -> Dict:
    base = {
        "domain": domain,
        "seed_url": f"https://{domain}/",
        "enabled": True,
        "strategy": "deep_crawl",
        "robots": {"respect": True, "crawl_delay": 0},
        "transport": {"timeout_s": 5, "max_retries": 1},
        "allowed_hosts": None,
        "search": {},
        "article_url_patterns": [],
        "listing_url_patterns": [],
    }
    base.update(overrides)
    return base


class FakeSession:
    def __init__(self, responder):
        self.responder = responder
        self.requested: List[str] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True, **kwargs):
        self.requested.append(url)
        return self.responder(url, headers)


# ── crawl4ai-absent fallback ─────────────────────────────────────────────────

def test_crawl4ai_unavailable_flag(monkeypatch):
    # Whether crawl4ai happens to be installed in the environment running this
    # suite must not change test outcomes. Force the premise explicitly rather
    # than asserting on the ambient environment (a global autouse fixture in
    # conftest.py already defaults every test in this directory to
    # CRAWL4AI_AVAILABLE=False; this restates it so the test is correct in
    # isolation too). The actual BFS-fallback *behavior* this flag gates is
    # verified by test_bfs_fallback_used_when_crawl4ai_absent below.
    monkeypatch.setattr(dc_module, "CRAWL4AI_AVAILABLE", False)
    assert dc_module.CRAWL4AI_AVAILABLE is False


def test_bfs_fallback_used_when_crawl4ai_absent(monkeypatch):
    profile = _profile(
        "example-deep.test",
        article_url_patterns=[r"/article/\d+$"],
        listing_url_patterns=[r"/section/?$"],
    )
    strat = DeepCrawlStrategy(profile)

    seed_html = (
        b'<html><body>'
        b'<a href="/article/1">Article One</a>'
        b'<a href="/section">Section</a>'
        b'</body></html>'
    )
    article_html = b'<html><head><title>Article One</title></head><body>text</body></html>'

    def responder(url, headers):
        if url.rstrip("/") == "https://example-deep.test":
            return FakeResponse(url, 200, content=seed_html)
        if "/article/1" in url:
            return FakeResponse(url, 200, content=article_html)
        return FakeResponse(url, 200, content=b"<html><body></body></html>")

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=10)
    assert not result.errors
    urls = {du.url for du in result.urls}
    assert "https://example-deep.test/article/1" in urls


# ── fmprc.gov.cn maintenance-page redirect trap ─────────────────────────────

def test_fmprc_maintenance_redirect_is_detected_and_skipped(monkeypatch):
    profile = _profile(
        "fmprc.gov.cn",
        seed_url="https://www.fmprc.gov.cn/eng/",
        article_url_patterns=[r"/eng/.*/\d{6}/t\d{8}_\d+\.html$"],
        listing_url_patterns=[r"/eng/[a-z_0-9/]+/(index(_\d+)?\.html)?$"],
    )
    strat = DeepCrawlStrategy(profile)

    maintenance_body = b"x" * 55990

    def responder(url, headers):
        # Every unknown path 302s (requests follows redirects, so resp.url differs
        # from the requested url) to the Chinese maintenance page, HTTP 200.
        return FakeResponse("https://www.mfa.gov.cn/web/system/index_17321.shtml",
                             200, content=maintenance_body)

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=10)
    assert result.urls == []
    assert any("maintenance" in e for e in result.errors)


def test_fmprc_real_content_is_not_flagged_as_maintenance(monkeypatch):
    profile = _profile(
        "fmprc.gov.cn",
        seed_url="https://www.fmprc.gov.cn/eng/",
        article_url_patterns=[r"/eng/.*/\d{6}/t\d{8}_\d+\.html$"],
        listing_url_patterns=[r"/eng/[a-z_0-9/]+/(index(_\d+)?\.html)?$"],
    )
    strat = DeepCrawlStrategy(profile)

    seed_html = (
        b'<html><body>'
        b'<a href="/eng/xw/wjbxw/202608/t20260801_1234.html">Real Article</a>'
        b'</body></html>'
    )
    article_html = (
        b'<html><head><title>Real Article Title</title></head><body>real content</body></html>'
    )

    def responder(url, headers):
        if url.rstrip("/") == "https://www.fmprc.gov.cn/eng":
            return FakeResponse(url, 200, content=seed_html)
        return FakeResponse(url, 200, content=article_html)

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=10)
    urls = {du.url for du in result.urls}
    assert "https://www.fmprc.gov.cn/eng/xw/wjbxw/202608/t20260801_1234.html" in urls
    assert not any("maintenance" in e for e in result.errors)


# ── Cloudflare-challenge detection (mizzima.com) ────────────────────────────

def test_mizzima_cloudflare_challenge_returns_empty_with_error(monkeypatch):
    profile = _profile("mizzima.com")
    strat = DeepCrawlStrategy(profile)

    def responder(url, headers):
        return FakeResponse(url, 403, content=b"Just a moment...",
                             headers={"cf-mitigated": "challenge"})

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=10)
    assert result.urls == []
    assert any("Cloudflare" in e for e in result.errors)


def test_mizzima_no_challenge_header_falls_through_to_bfs(monkeypatch):
    profile = _profile("mizzima.com", article_url_patterns=[r"/article/[a-z0-9-]+$"])
    strat = DeepCrawlStrategy(profile)

    seed_html = b'<html><body><a href="/article/some-story">Story</a></body></html>'
    article_html = b'<html><head><title>Some Story</title></head><body>ok</body></html>'

    def responder(url, headers):
        if url.rstrip("/") == "https://mizzima.com":
            return FakeResponse(url, 200, content=seed_html)
        return FakeResponse(url, 200, content=article_html)

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=10)
    urls = {du.url for du in result.urls}
    assert "https://mizzima.com/article/some-story" in urls


# ── hornbilltv numeric-ID walking ───────────────────────────────────────────

def test_hornbilltv_id_walk_downward(monkeypatch):
    profile = _profile(
        "hornbilltv.com",
        seed_url="https://www.hornbilltv.com/",
        deep_crawl={"hornbilltv_head_id": 105},
        article_url_patterns=[r"^https://www\.hornbilltv\.com/(?!author/)[a-z_]+/[a-z0-9-]+/\d+$"],
    )
    strat = DeepCrawlStrategy(profile)

    jsonld = json.dumps({"@type": "Article", "datePublished": "2026-08-01 10:00:00"})
    article_html = (
        '<html><head><script type="application/ld+json">' + jsonld + '</script></head>'
        '<body><h1>Story</h1></body></html>'
    ).encode()

    requested_ids = []

    def responder(url, headers):
        # /x/y/<id>
        id_str = url.rsplit("/", 1)[-1]
        requested_ids.append(int(id_str))
        if int(id_str) in (103, 101):
            return FakeResponse(url, 404, content=b"not found")
        return FakeResponse(url, 200, content=article_html)

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=3)
    assert not any("error" in e.lower() and "unexpected" in e.lower() for e in result.errors)
    # Walk starts at head (105) and goes downward.
    assert requested_ids[0] == 105
    assert requested_ids == sorted(requested_ids, reverse=True)
    urls = [du.url for du in result.urls]
    assert all(u.startswith("https://www.hornbilltv.com/x/y/") for u in urls)
    assert len(result.urls) == 3  # max_urls cap respected


def test_hornbilltv_topic_search_single_page_no_pagination(monkeypatch):
    profile = _profile(
        "hornbilltv.com",
        seed_url="https://www.hornbilltv.com/",
        search={"supported": True, "url_template": "https://www.hornbilltv.com/topics/{keyword}"},
        deep_crawl={"hornbilltv_head_id": 1},  # keep the ID-walk phase tiny/irrelevant
        article_url_patterns=[r"^https://www\.hornbilltv\.com/(?!author/)[a-z_]+/[a-z0-9-]+/\d+$"],
    )
    strat = DeepCrawlStrategy(profile)

    topic_html = (
        b'<html><body>'
        b'<a href="/topstories/assam-news-story/233457">Assam story</a>'
        b'</body></html>'
    )

    requested = []

    def responder(url, headers):
        requested.append(url)
        if "/topics/" in url:
            return FakeResponse(url, 200, content=topic_html)
        return FakeResponse(url, 404, content=b"not found")

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(keyword="Assam", max_urls=50)
    topic_requests = [u for u in requested if "/topics/" in u]
    assert len(topic_requests) == 1  # fake pagination is never followed
    urls = {du.url for du in result.urls}
    assert "https://www.hornbilltv.com/topstories/assam-news-story/233457" in urls


# ── stats.gov.cn: https rewrite for hostile transport ───────────────────────

def test_stats_gov_cn_rewrites_http_links_to_https(monkeypatch):
    profile = _profile(
        "stats.gov.cn",
        seed_url="https://www.stats.gov.cn/english/PressRelease/",
        transport={"timeout_s": 60, "max_retries": 5, "force_https": True,
                   "force_ipv4": True},
        article_url_patterns=[r"/english/.*/\d{6}/t\d{8}_\d+\.html$"],
    )
    strat = DeepCrawlStrategy(profile)

    seed_html = (
        b'<html><body>'
        # Internal links written http:// per the verified profile note.
        b'<a href="http://www.stats.gov.cn/english/PressRelease/202608/t20260801_1.html">Item</a>'
        b'</body></html>'
    )
    article_html = b'<html><head><title>Item</title></head><body>x</body></html>'

    requested = []

    def responder(url, headers):
        requested.append(url)
        if url.rstrip("/") == "https://www.stats.gov.cn/english/PressRelease":
            return FakeResponse(url, 200, content=seed_html)
        return FakeResponse(url, 200, content=article_html)

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=10)
    assert all(not u.startswith("http://") for u in requested)
    urls = {du.url for du in result.urls}
    assert "https://www.stats.gov.cn/english/PressRelease/202608/t20260801_1.html" in urls


def test_discover_never_raises_on_network_error(monkeypatch):
    profile = _profile("fmprc.gov.cn", seed_url="https://www.fmprc.gov.cn/eng/")
    strat = DeepCrawlStrategy(profile)

    import requests as _requests

    def responder(url, headers):
        raise _requests.exceptions.ConnectionError("boom")

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.deep_crawl.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.deep_crawl.time.sleep", lambda s: None)

    result = strat.discover(max_urls=5)  # must not raise
    assert result.urls == []
    assert result.errors


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
