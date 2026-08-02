"""
backend/test_queue_manager_discovery_wiring.py — unit tests for the adaptive
discovery layer's wiring into queue_manager.run_direct_discovery() and
queue_manager._process_single_keyword().

No live network, no real Postgres: strategy dispatch is monkeypatched at the
backend.discovery.base.get_strategy boundary, Crawler is monkeypatched wherever
link-expansion / site-search-detector could otherwise hit the network, and DB
access goes through an isolated in-memory SQLite session (queue_manager.SessionLocal
is monkeypatched) rather than the module-level SessionLocal, which in this
environment can resolve to a real reachable PostgreSQL instance.

Run with: python3 -m pytest backend/test_queue_manager_discovery_wiring.py -v
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.queue_manager as qm
from backend.database import Base
from backend.discovery.base import DiscoveredURL, DiscoveryResult
from backend.models import CrawledURL, KeywordProgress, SearchQuery  # noqa: F401 - registers tables


# ── run_direct_discovery: profile-driven dispatch ─────────────────────────────

def test_run_direct_discovery_profile_driven_dispatch_preserves_metadata(monkeypatch):
    """A domain with a registered, enabled profile must go through
    discovery_base.get_strategy(), and the DiscoveredURL metadata that strategy
    produces (title, published_at, relevance_score) must survive unchanged all the
    way back out of run_direct_discovery - this is the crux of the whole task."""
    profile = {"domain": "niice.org.np", "strategy": "wp_api", "enabled": True}
    monkeypatch.setattr(qm, "_load_site_profiles", lambda: {"niice.org.np": profile})

    published = datetime(2026, 7, 30, tzinfo=timezone.utc)
    fake_result = DiscoveryResult(
        urls=[DiscoveredURL(url="https://niice.org.np/post/1", source="wp_api",
                             title="Headline 1", published_at=published,
                             relevance_score=42.0)],
        errors=[], strategy="wp_api", domain="niice.org.np",
    )
    fake_strategy = MagicMock()
    fake_strategy.discover.return_value = fake_result
    monkeypatch.setattr(qm.discovery_base, "get_strategy", lambda p: fake_strategy)

    query = MagicMock()
    query.id = 101
    query.date_range_start = None
    query.proxy_url = None
    query.ignore_robots = False

    candidates, domains = qm.run_direct_discovery(
        ["https://niice.org.np/"], query, keyword="climate"
    )

    assert domains == {"niice.org.np"}
    urls = candidates["https://niice.org.np/"]
    assert len(urls) == 1
    du = urls[0]
    assert isinstance(du, DiscoveredURL)
    assert du.url == "https://niice.org.np/post/1"
    assert du.title == "Headline 1"
    assert du.published_at == published
    assert du.relevance_score == 42.0

    fake_strategy.discover.assert_called_once()
    _, kwargs = fake_strategy.discover.call_args
    assert kwargs["keyword"] == "climate"


def test_run_direct_discovery_caps_primary_discovery_per_domain(monkeypatch):
    """Fix A regression test (HANDOFF.md item #2 / new Bug A from search_id=144):
    a profile-driven strategy.discover() call that genuinely succeeds and returns
    hundreds of real candidates for one domain must be trimmed against the SAME
    shared domain_candidate_budget that already gates search_web()/
    SiteSearchDetector - it was previously completely uncapped. Confirmed live:
    newslivetv.com alone returned 309 candidates from primary discovery in one
    61-source run."""
    monkeypatch.setattr(qm, "_MAX_CANDIDATES_PER_DOMAIN", 20)

    profile = {"domain": "newslivetv.com", "strategy": "sitemap", "enabled": True}
    monkeypatch.setattr(qm, "_load_site_profiles", lambda: {"newslivetv.com": profile})

    fake_result = DiscoveryResult(
        urls=[DiscoveredURL(url=f"https://newslivetv.com/post-{i}", source="sitemap")
              for i in range(300)],
        errors=[], strategy="sitemap", domain="newslivetv.com",
    )
    fake_strategy = MagicMock()
    fake_strategy.discover.return_value = fake_result
    monkeypatch.setattr(qm.discovery_base, "get_strategy", lambda p: fake_strategy)

    query = MagicMock()
    query.id = 104
    query.date_range_start = None
    query.proxy_url = None
    query.ignore_robots = False

    domain_candidate_budget: dict = {}
    domain_budget_lock = threading.Lock()

    candidates, domains = qm.run_direct_discovery(
        ["https://newslivetv.com/"], query, keyword="china",
        domain_candidate_budget=domain_candidate_budget,
        domain_budget_lock=domain_budget_lock,
    )

    assert domains == {"newslivetv.com"}
    urls = candidates["https://newslivetv.com/"]
    assert len(urls) == 20, (
        f"expected primary discovery's 300 candidates to be trimmed to the shared "
        f"budget (cap=20), got {len(urls)}"
    )
    assert domain_candidate_budget["newslivetv.com"] == 20


def test_run_direct_discovery_shares_budget_between_primary_discovery_and_search_web(monkeypatch):
    """The shared budget must actually be shared: a domain's primary-discovery
    contribution and its later search_web/SiteSearchDetector contribution (within
    the same job) must draw down the SAME counter, not two independent ones -
    otherwise a domain could still reach 2x the intended cap by combining both
    mechanisms."""
    monkeypatch.setattr(qm, "_MAX_CANDIDATES_PER_DOMAIN", 10)

    profile = {"domain": "newslivetv.com", "strategy": "sitemap", "enabled": True}
    monkeypatch.setattr(qm, "_load_site_profiles", lambda: {"newslivetv.com": profile})

    fake_result = DiscoveryResult(
        urls=[DiscoveredURL(url=f"https://newslivetv.com/post-{i}", source="sitemap")
              for i in range(7)],
        errors=[], strategy="sitemap", domain="newslivetv.com",
    )
    fake_strategy = MagicMock()
    fake_strategy.discover.return_value = fake_result
    monkeypatch.setattr(qm.discovery_base, "get_strategy", lambda p: fake_strategy)

    query = MagicMock()
    query.id = 105
    query.date_range_start = None
    query.proxy_url = None
    query.ignore_robots = False

    domain_candidate_budget: dict = {}
    domain_budget_lock = threading.Lock()

    # Primary discovery draws 7 of the 10-candidate budget.
    qm.run_direct_discovery(
        ["https://newslivetv.com/"], query, keyword="china",
        domain_candidate_budget=domain_candidate_budget,
        domain_budget_lock=domain_budget_lock,
    )
    assert domain_candidate_budget["newslivetv.com"] == 7

    # A later _trim_to_domain_budget() call (as search_web/SiteSearchDetector
    # results would go through) must only have 3 slots left.
    remaining_trimmed = qm._trim_to_domain_budget(
        "https://newslivetv.com/", [f"https://newslivetv.com/web-{i}" for i in range(5)],
        domain_candidate_budget, domain_budget_lock,
    )
    assert len(remaining_trimmed) == 3
    assert domain_candidate_budget["newslivetv.com"] == 10


def test_run_direct_discovery_no_profile_falls_back_to_legacy(monkeypatch):
    """A domain absent from config/site_profiles.json must fall through to the
    unchanged legacy sitemap/feed/link-expansion logic, still wrapped as
    DiscoveredURL so the return type stays uniform across every branch."""
    monkeypatch.setattr(qm, "_load_site_profiles", lambda: {})

    mock_crawler = MagicMock()
    mock_crawler.fetch_page.return_value = (
        '<html><body><a href="/a">a</a><a href="/b">b</a>'
        '<a href="https://other.com/x">external</a></body></html>'
    )
    monkeypatch.setattr(qm, "Crawler", lambda *a, **kw: mock_crawler)

    query = MagicMock()
    query.id = 102
    query.proxy_url = None
    query.ignore_robots = True

    candidates, domains = qm.run_direct_discovery(["https://example.com/page"], query)

    assert domains == {"example.com"}
    urls = candidates["https://example.com/page"]
    assert urls, "legacy link-expansion should still return candidates"
    assert all(isinstance(u, DiscoveredURL) for u in urls)
    assert all(u.source == "legacy" for u in urls)
    got = {u.url for u in urls}
    assert got == {
        "https://example.com/page",
        "https://example.com/a",
        "https://example.com/b",
    }
    # every metadata field is unknown on the legacy path - never fabricated
    assert all(u.published_at is None for u in urls)


def test_run_direct_discovery_search_strategy_without_keyword_falls_back(monkeypatch):
    """A `search`-strategy profile is useless without a keyword (its discover()
    errors out immediately). When run_direct_discovery is called keyword-
    independently, it must fall back to the legacy path instead of calling
    discover() with an empty keyword and silently getting nothing."""
    profile = {"domain": "example.com", "strategy": "search", "enabled": True}
    monkeypatch.setattr(qm, "_load_site_profiles", lambda: {"example.com": profile})

    fake_strategy = MagicMock()
    monkeypatch.setattr(qm.discovery_base, "get_strategy", lambda p: fake_strategy)

    mock_crawler = MagicMock()
    mock_crawler.fetch_page.return_value = "<html><body></body></html>"
    monkeypatch.setattr(qm, "Crawler", lambda *a, **kw: mock_crawler)

    query = MagicMock()
    query.id = 103
    query.proxy_url = None
    query.ignore_robots = False

    candidates, domains = qm.run_direct_discovery(
        ["https://example.com/"], query, keyword=None
    )

    fake_strategy.discover.assert_not_called()
    urls = candidates["https://example.com/"]
    assert all(u.source == "legacy" for u in urls)
    assert {"https://example.com/"} <= {u.url for u in urls}


# ── _process_single_keyword: metadata plumbing + stale-drop + row population ──

@pytest.fixture
def isolated_db(monkeypatch):
    """Bind queue_manager.SessionLocal to a private in-memory SQLite engine so
    tests never touch whatever real database backend.database resolved to."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(qm, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def _direct_query_mock(**overrides):
    query = MagicMock()
    query.source_type = "direct"
    query.direct_urls = "https://example.com/"
    query.domains_filter = None
    query.date_range_start = None
    query.proxy_url = None
    query.ignore_robots = True
    query.engine = "fast"
    for k, v in overrides.items():
        setattr(query, k, v)
    return query


def test_stale_url_dropped_before_crawledurl_row_created(monkeypatch, isolated_db):
    old_published = datetime.now(timezone.utc) - timedelta(days=365)
    fresh_published = datetime.now(timezone.utc) - timedelta(days=1)

    stale_du = DiscoveredURL(url="https://example.com/old", source="wp_api",
                              title="Old article", published_at=old_published,
                              relevance_score=10.0)
    fresh_du = DiscoveredURL(url="https://example.com/new", source="wp_api",
                              title="New article", published_at=fresh_published,
                              relevance_score=99.0)
    unknown_du = DiscoveredURL(url="https://example.com/unknown-date", source="legacy")

    monkeypatch.setattr(
        qm, "run_direct_discovery",
        lambda urls, query, keyword=None, **kwargs: (
            {"https://example.com/": [stale_du, fresh_du, unknown_du]},
            {"example.com"},
        ),
    )

    query = _direct_query_mock()

    result = qm._process_single_keyword(
        search_id=201,
        kw="__config__",  # keyword-independent: skips SiteSearchDetector / search_web,
                           # which would otherwise require real network mocking
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=1,
        discover_only=True,
    )

    assert "https://example.com/old" not in result
    assert "https://example.com/new" in result
    assert "https://example.com/unknown-date" in result

    db = isolated_db()
    try:
        rows = {r.url: r for r in db.query(CrawledURL).filter(CrawledURL.search_id == 201).all()}
    finally:
        db.close()

    assert "https://example.com/old" not in rows, (
        "a DiscoveredURL with a published_at older than the cutoff must be dropped "
        "before a CrawledURL row is ever created"
    )
    assert "https://example.com/new" in rows
    assert "https://example.com/unknown-date" in rows, (
        "a DiscoveredURL with published_at=None (unknown date) must NOT be dropped"
    )


def test_crawledurl_rows_populated_from_discovered_url_metadata(monkeypatch, isolated_db):
    fresh_published = datetime.now(timezone.utc) - timedelta(days=1)
    du = DiscoveredURL(url="https://example.com/new", source="wp_api",
                        title="New article", published_at=fresh_published,
                        relevance_score=87.5)
    unknown_du = DiscoveredURL(url="https://example.com/unknown-date", source="legacy")

    monkeypatch.setattr(
        qm, "run_direct_discovery",
        lambda urls, query, keyword=None, **kwargs: (
            {"https://example.com/": [du, unknown_du]},
            {"example.com"},
        ),
    )

    query = _direct_query_mock()

    qm._process_single_keyword(
        search_id=202,
        kw="__config__",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=1,
        discover_only=True,
    )

    db = isolated_db()
    try:
        rows = {r.url: r for r in db.query(CrawledURL).filter(CrawledURL.search_id == 202).all()}
    finally:
        db.close()

    new_row = rows["https://example.com/new"]
    assert new_row.title == "New article"
    assert new_row.relevance_score == 87.5
    assert new_row.discovered_at is not None
    # published_at (made tz-aware UTC) should have been used, not "now"
    assert abs((new_row.discovered_at.replace(tzinfo=timezone.utc) - fresh_published).total_seconds()) < 5

    unknown_row = rows["https://example.com/unknown-date"]
    assert unknown_row.title is None
    assert unknown_row.relevance_score == 0.0
    assert unknown_row.discovered_at is not None  # defaults to now(), never left null


def test_keyword_is_threaded_through_to_run_direct_discovery_call_site(monkeypatch, isolated_db):
    """The per-keyword call site (inside the 'Fallback implementation' branch) must
    pass the active keyword through to run_direct_discovery, unlike the
    keyword-independent pre-discovery call in process_search_query."""
    captured = {}

    def fake_run_direct_discovery(urls, query, keyword=None, **kwargs):
        captured["keyword"] = keyword
        return {"https://example.com/": []}, set()

    monkeypatch.setattr(qm, "run_direct_discovery", fake_run_direct_discovery)

    # kw is a real keyword here (not "__config__"), so the SiteSearchDetector probe
    # in the "Fallback implementation" branch will run - keep it off the network.
    mock_crawler = MagicMock()
    mock_crawler.fetch_page.return_value = "<html><body></body></html>"
    monkeypatch.setattr(qm, "Crawler", lambda *a, **kw: mock_crawler)

    query = _direct_query_mock()

    qm._process_single_keyword(
        search_id=203,
        kw="myanmar coup",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=1,
        discover_only=True,
    )

    assert captured["keyword"] == "myanmar coup"


def test_site_restricted_search_respects_shared_domain_budget(monkeypatch, isolated_db):
    """Regression test for the pilot-run finding: search_web(f'{kw} site:{domain}')
    runs independently per keyword and returns genuinely distinct real URLs each
    time, so nothing upstream deduplicates it - a single domain's candidate count
    can explode across a multi-keyword job (a live pilot produced 324 distinct
    URLs for one domain this way, 99.5% of which then failed the crawl watchdog,
    since per-domain fetch rate limiting is shared/serialized at ~1 req/sec
    regardless of queue depth). domain_candidate_budget/domain_budget_lock (shared
    across ALL keyword calls, exactly as process_search_query wires them) must cap
    a domain's TOTAL contribution across the whole job, not just per-keyword."""
    monkeypatch.setattr(qm, "_MAX_CANDIDATES_PER_DOMAIN", 5)

    call_count = {"n": 0}

    def fake_search_web(query_str, max_results=50, tor_proxies=None):
        # Each call (i.e. each keyword) returns 4 UNIQUE urls - simulating
        # search_web's real behaviour of distinct results per keyword query.
        call_count["n"] += 1
        return [f"https://example.com/kw{call_count['n']}-article{i}" for i in range(4)]

    monkeypatch.setattr(qm, "search_web", fake_search_web)
    monkeypatch.setattr(qm, "run_direct_discovery",
                         lambda urls, query, keyword=None, **kwargs: ({"https://example.com/": []}, {"example.com"}))

    mock_crawler = MagicMock()
    mock_crawler.fetch_page.return_value = "<html><body></body></html>"
    monkeypatch.setattr(qm, "Crawler", lambda *a, **kw: mock_crawler)

    query = _direct_query_mock()
    domain_candidate_budget: dict = {}
    domain_budget_lock = threading.Lock()

    # Two "keywords" sharing the same budget dict/lock, exactly as
    # process_search_query creates ONE shared pair and passes it to every
    # _process_single_keyword call across the whole job.
    for kw in ("keyword-one", "keyword-two"):
        qm._process_single_keyword(
            search_id=204,
            kw=kw,
            query=query,
            languages_filter=None,
            seen_content_hashes=set(),
            seen_simhashes=[],
            seen_lock=threading.Lock(),
            total_keyword_count=2,
            discover_only=True,
            domain_candidate_budget=domain_candidate_budget,
            domain_budget_lock=domain_budget_lock,
        )

    assert call_count["n"] == 2, "both keywords should have called search_web"

    db = isolated_db()
    try:
        rows = db.query(CrawledURL).filter(
            CrawledURL.search_id == 204, CrawledURL.domain == "example.com").all()
    finally:
        db.close()

    # 4 (keyword-one, under budget) + 1 (keyword-two, trimmed to fill the
    # remaining budget of 5-4=1) = 5 total, NOT 8 (4+4 uncapped).
    assert len(rows) == 5, (
        f"expected the shared budget (cap=5) to trim the second keyword's "
        f"contribution down to what's left, got {len(rows)} rows: "
        f"{sorted(r.url for r in rows)}"
    )
    assert domain_candidate_budget["example.com"] == 5


def test_site_search_detector_shares_the_same_domain_budget_as_web_search(monkeypatch, isolated_db):
    """The live pilot run's actual failure mode: thebalochistanpost.net barely
    dropped (324 -> 319 distinct candidates) after capping search_web() alone,
    because SiteSearchDetector.discover() is a SECOND per-keyword-repeated
    mechanism (backend/site_search_detector.py, invoked separately in
    _process_single_keyword's 'Check site-native search detection' block) that
    was never subject to the cap. Both mechanisms must draw from the SAME shared
    per-domain budget, since together they're what inflated one domain to 300+
    real candidates across a 10-keyword job."""
    monkeypatch.setattr(qm, "_MAX_CANDIDATES_PER_DOMAIN", 6)

    # search_web contributes 3 unique urls per keyword call.
    web_call_count = {"n": 0}

    def fake_search_web(query_str, max_results=50, tor_proxies=None):
        web_call_count["n"] += 1
        return [f"https://example.com/web-kw{web_call_count['n']}-{i}" for i in range(3)]

    # SiteSearchDetector.discover() ALSO contributes 3 unique urls per keyword call,
    # via a completely separate code path.
    detector_call_count = {"n": 0}

    class FakeDetector:
        def __init__(self, crawler):
            pass

        def discover(self, url, keyword, engine, ignore_robots):
            detector_call_count["n"] += 1
            return [f"https://example.com/detector-kw{detector_call_count['n']}-{i}" for i in range(3)]

    monkeypatch.setattr(qm, "search_web", fake_search_web)
    monkeypatch.setattr(qm, "SiteSearchDetector", FakeDetector)
    monkeypatch.setattr(qm, "run_direct_discovery",
                         lambda urls, query, keyword=None, **kwargs: ({"https://example.com/": []}, {"example.com"}))

    mock_crawler = MagicMock()
    mock_crawler.fetch_page.return_value = "<html><body></body></html>"
    monkeypatch.setattr(qm, "Crawler", lambda *a, **kw: mock_crawler)

    query = _direct_query_mock(direct_urls="https://example.com/")
    domain_candidate_budget: dict = {}
    domain_budget_lock = threading.Lock()

    for kw in ("keyword-one", "keyword-two"):
        qm._process_single_keyword(
            search_id=205,
            kw=kw,
            query=query,
            languages_filter=None,
            seen_content_hashes=set(),
            seen_simhashes=[],
            seen_lock=threading.Lock(),
            total_keyword_count=2,
            pre_discovered_candidates={"https://example.com/": []},
            pre_discovered_domains={"example.com"},
            discover_only=True,
            domain_candidate_budget=domain_candidate_budget,
            domain_budget_lock=domain_budget_lock,
        )

    assert web_call_count["n"] == 2 and detector_call_count["n"] == 2

    db = isolated_db()
    try:
        rows = db.query(CrawledURL).filter(
            CrawledURL.search_id == 205, CrawledURL.domain == "example.com").all()
    finally:
        db.close()

    # Uncapped this would be 2 keywords x (3 web + 3 detector) = 12. The shared
    # budget (cap=6) must hold the total to 6 regardless of WHICH mechanism
    # contributed each candidate.
    assert len(rows) == 6, (
        f"expected the shared budget (cap=6) to hold the combined total from "
        f"BOTH search_web and SiteSearchDetector to 6, got {len(rows)} rows: "
        f"{sorted(r.url for r in rows)}"
    )
    assert domain_candidate_budget["example.com"] == 6


# ── _fair_trim_candidates_across_domains: fair candidate-cap trim (Fix B) ──────

def _test_get_domain(url):
    """Mirrors _process_single_keyword's local get_domain() closure exactly -
    duplicated here only because that closure isn't accessible outside the
    function, per the instruction to reuse get_domain() at the real call site
    (queue_manager.py) rather than redefine it there."""
    from urllib.parse import urlparse as _urlparse
    d = _urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def test_fair_trim_gives_every_domain_representation_not_just_the_biggest():
    """Fix B regression test for the new Bug B found via search_id=144: a naive
    candidate_urls[:_max_candidates] trim keeps whichever domain happens to be
    biggest/earliest in list-insertion order and can completely crowd out every
    other domain. Confirmed live: SiteSearchDetector found substantial real
    candidate counts for dozens of domains, but only 9 of 61 domains ended up
    with ANY row in the final CrawledURL table for the whole job. Construct the
    same shape here: one domain (A) massively dominates, three others (B, C, D)
    each contribute a small, reasonable amount - every domain must still get at
    least some representation in the trimmed result."""
    candidate_urls = (
        [f"https://domain-a.com/article-{i}" for i in range(480)]
        + [f"https://domain-b.com/article-{i}" for i in range(10)]
        + [f"https://domain-c.com/article-{i}" for i in range(10)]
        + [f"https://domain-d.com/article-{i}" for i in range(10)]
    )
    assert len(candidate_urls) == 510

    trimmed = qm._fair_trim_candidates_across_domains(candidate_urls, 20, _test_get_domain)

    assert len(trimmed) == 20
    domains_present = {_test_get_domain(u) for u in trimmed}
    assert domains_present == {"domain-a.com", "domain-b.com", "domain-c.com", "domain-d.com"}, (
        f"expected every domain to have at least one URL in the trimmed result, "
        f"got only: {domains_present}"
    )
    # Round-robin over 4 domains for 20 slots -> each domain gets exactly 5,
    # since B/C/D (10 each) never run out before the cap is reached.
    from collections import Counter
    counts = Counter(_test_get_domain(u) for u in trimmed)
    assert counts == {"domain-a.com": 5, "domain-b.com": 5, "domain-c.com": 5, "domain-d.com": 5}


def test_fair_trim_preserves_each_domains_own_relative_order():
    """Within a domain's own slice, the fair trim must not reorder/shuffle - it
    should pop each domain's own candidates in their original relative order
    (round-robin ACROSS domains, not WITHIN one)."""
    candidate_urls = (
        [f"https://domain-a.com/article-{i}" for i in range(5)]
        + [f"https://domain-b.com/article-{i}" for i in range(5)]
    )
    trimmed = qm._fair_trim_candidates_across_domains(candidate_urls, 4, _test_get_domain)
    a_urls = [u for u in trimmed if _test_get_domain(u) == "domain-a.com"]
    b_urls = [u for u in trimmed if _test_get_domain(u) == "domain-b.com"]
    assert a_urls == ["https://domain-a.com/article-0", "https://domain-a.com/article-1"]
    assert b_urls == ["https://domain-b.com/article-0", "https://domain-b.com/article-1"]


def test_fair_trim_is_a_no_op_when_already_under_the_cap():
    candidate_urls = [f"https://domain-a.com/article-{i}" for i in range(5)]
    trimmed = qm._fair_trim_candidates_across_domains(candidate_urls, 500, _test_get_domain)
    assert trimmed == candidate_urls
    assert trimmed is candidate_urls  # no copy needed when nothing is trimmed


# ── _reserve_domain_slot: replaces the busy-wait rate limiter ─────────────────

def test_reserve_domain_slot_spaces_same_domain_requests_correctly_under_contention():
    """Regression test for the pilot-run finding: the previous while-True
    busy-wait design (every thread re-acquiring ONE lock shared across every
    domain in the job, checking, sleeping, rechecking) caused ~84% watchdog
    failures under real concurrency despite an idle 48-core host and sub-2s
    direct response times - ruling out hardware/network as the cause. 20
    threads racing for the same domain's rate-limited slot must each get a
    UNIQUE, correctly-spaced slot (no two closer than rate_limit_s, and slot
    N+1 no earlier than slot N + rate_limit_s), with no double-booking."""
    last_crawl_registry: Dict[str, float] = {}
    slot_lock_registry: Dict[str, threading.Lock] = {}
    meta_lock = threading.Lock()
    rate_limit_s = 0.05  # fast for test speed; the spacing logic is rate-independent
    results_lock = threading.Lock()
    slot_times = []

    def worker():
        ok = qm._reserve_domain_slot(
            "example.com", rate_limit_s, last_crawl_registry,
            slot_lock_registry, meta_lock, lambda: False)
        t = time.time()
        with results_lock:
            slot_times.append((t, ok))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(slot_times) == 20
    assert all(ok for _, ok in slot_times)
    times = sorted(t for t, _ in slot_times)
    gaps = [b - a for a, b in zip(times, times[1:])]
    # Each consecutive pair of completions must be spaced at least ~rate_limit_s
    # apart (small tolerance for scheduling jitter) - proves no double-booking.
    assert all(g >= rate_limit_s * 0.8 for g in gaps), (
        f"found a gap smaller than the rate limit allows: {gaps}")


def test_reserve_domain_slot_different_domains_never_block_each_other():
    """A slow/contended domain must not delay threads waiting on a DIFFERENT
    domain - the whole point of moving from one global lock to per-domain locks."""
    last_crawl_registry: Dict[str, float] = {}
    slot_lock_registry: Dict[str, threading.Lock] = {}
    meta_lock = threading.Lock()

    # A domain's FIRST-ever reservation never waits (no prior slot to space
    # against) - seed a "just used" timestamp so the NEXT reservation for
    # busy.com genuinely has to wait out its rate limit, the same way a second
    # candidate for an already-active domain would in the real pipeline.
    last_crawl_registry["busy.com"] = time.time()
    busy_done = threading.Event()

    def saturate_busy_domain():
        qm._reserve_domain_slot("busy.com", 5.0, last_crawl_registry,
                                 slot_lock_registry, meta_lock, lambda: False)
        busy_done.set()

    t_busy = threading.Thread(target=saturate_busy_domain)
    t_busy.start()
    time.sleep(0.05)  # let it acquire and start its ~5s wait

    t0 = time.time()
    ok = qm._reserve_domain_slot("quiet.com", 0.01, last_crawl_registry,
                                  slot_lock_registry, meta_lock, lambda: False)
    elapsed = time.time() - t0

    assert ok
    assert elapsed < 1.0, (
        f"quiet.com's reservation took {elapsed:.2f}s while busy.com is "
        f"mid-wait on a 5s slot - per-domain locks should make these independent"
    )
    assert not busy_done.is_set(), "busy.com should still be waiting its 5s slot"
    t_busy.join(timeout=10)


def test_reserve_domain_slot_respects_check_stopped():
    """A job-stop signal firing mid-wait must interrupt the sleep promptly and
    return False, matching the original while-True loop's abort behaviour."""
    last_crawl_registry: Dict[str, float] = {}
    slot_lock_registry: Dict[str, threading.Lock] = {}
    meta_lock = threading.Lock()

    # Seed a recent slot so this reservation actually has to wait (a fresh
    # domain's first reservation never waits - see the sibling test above).
    last_crawl_registry["example.com"] = time.time()

    stop_after = time.time() + 0.1
    ok = qm._reserve_domain_slot(
        "example.com", 5.0, last_crawl_registry, slot_lock_registry, meta_lock,
        check_stopped=lambda: time.time() >= stop_after)

    assert ok is False


def test_crawl_concurrency_semaphore_caps_simultaneous_work(monkeypatch):
    """Regression test for the second, deeper root cause found after the domain
    budget and rate-limiter fixes still left ~95% of a real pilot run failing the
    watchdog: nothing capped TOTAL concurrent crawl_url_task fetch+parse work
    job-wide, only per-keyword worker-pool size - so keyword_count x
    workers_per_keyword multiplied unboundedly toward ~120+ simultaneous Python
    threads, and direct reproduction proved Python's GIL cannot service that many
    concurrent threads doing real I/O+CPU-bound work efficiently (120 threads:
    13% of a 505-URL batch completed in 130s; 30 threads: 29%, zero over the
    watchdog). This test proves the semaphore actually bounds peak concurrency to
    KS_MAX_CONCURRENT_CRAWLS regardless of how many threads call crawl_url_task
    at once."""
    monkeypatch.setattr(qm, "_CRAWL_CONCURRENCY_SEMAPHORE", threading.BoundedSemaphore(3))

    peak = {"n": 0, "max": 0}
    peak_lock = threading.Lock()

    class SlowMockCrawler:
        def __init__(self, *a, **kw):
            pass

        def fetch_page(self, url, engine, ignore_robots):
            with peak_lock:
                peak["n"] += 1
                peak["max"] = max(peak["max"], peak["n"])
            time.sleep(0.1)
            with peak_lock:
                peak["n"] -= 1
            return "<html><body>irrelevant</body></html>"

        def analyze_page(self, **kw):
            return {"matched": False}

        def close(self):
            pass

    monkeypatch.setattr(qm, "Crawler", SlowMockCrawler)

    # sqlite:///:memory: is per-connection - real worker threads each calling
    # SessionLocal() would otherwise get their own empty database. StaticPool
    # forces every thread to share the ONE actual connection/database, which
    # isolated_db's plain "sqlite:///:memory:" engine (used by every other test
    # in this file) does not do, since those tests only ever touch the DB from
    # the main thread.
    from sqlalchemy.pool import StaticPool
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(qm, "SessionLocal", TestSessionLocal)

    db = TestSessionLocal()
    q = SearchQuery(id=901, keyword="test", status="processing")
    db.add(q)
    urls = []
    for i in range(10):
        u = CrawledURL(search_id=901, url=f"https://example.com/{i}", domain="example.com", status="pending")
        db.add(u)
        urls.append(u)
    db.commit()
    for u in urls:
        db.refresh(u)
    url_ids = [u.id for u in urls]
    db.close()

    threads = [
        threading.Thread(target=qm.crawl_url_task, kwargs=dict(
            url_id=uid, search_id=901, keyword="test", match_type="phrase",
            case_sensitive=False, exact_match=False, engine="fast",
            ignore_robots=True, domain_rate_limit=0.0,
        ))
        for uid in url_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert peak["max"] <= 3, (
        f"peak concurrent fetch+parse work was {peak['max']}, expected the "
        f"semaphore to cap it at 3"
    )
    assert peak["max"] >= 2, "expected genuine overlap to occur, not accidental full serialization"


# ── per-URL write isolation (search_id=148: 'china'/'POJK' NUL-byte crash) ────
#
# Root cause: Postgres text/varchar columns cannot store a literal NUL (0x00)
# byte at all. Some scraped page's content contained one, db.flush() raised
# "A string literal cannot contain NUL (0x00) characters.", and - because it
# wasn't caught locally - the exception propagated out of the ENTIRE
# while-pending result-processing loop, out of _process_single_keyword itself,
# and was only caught by the function's outer except-handler, which marked the
# WHOLE keyword "failed" and discarded every other result it would otherwise
# have produced. Two fixes: (1) _sanitize_for_db() strips NUL bytes from
# result values before they're ever assigned, eliminating the NUL-byte case
# specifically; (2) the per-URL write unit of work is wrapped in its own
# try/except so that ANY other unexpected write failure isolates to that one
# row instead of killing the whole keyword.

def test_nul_byte_in_crawl_result_is_sanitized_before_write(monkeypatch, isolated_db):
    """Part 1 regression test for search_id=148. A crawl_url_task result
    containing a literal NUL byte in full_content/title must not raise when
    written, the NUL byte must actually be gone from what lands in the row,
    and the row must end up with a sane, non-stuck status."""
    db = isolated_db()
    q = SearchQuery(id=301, keyword="china", status="processing")
    db.add(q)
    kp = KeywordProgress(search_query_id=301, keyword="china", status="pending")
    db.add(kp)
    u = CrawledURL(search_id=301, url="https://example.com/nul-article",
                    domain="example.com", status="pending")
    db.add(u)
    db.commit()
    db.refresh(u)
    url_id = u.id
    db.close()

    def fake_crawl_url_task(url_id, **kwargs):
        return url_id, {
            "status": "matched",
            "full_content": "Beijing said \x00 today that...",
            "title": "China\x00 news",
            "matched_keywords": '["china"]',
        }

    monkeypatch.setattr(qm, "crawl_url_task", fake_crawl_url_task)

    query = _direct_query_mock(direct_urls="https://example.com/nul-article")

    # Must not raise.
    qm._process_single_keyword(
        search_id=301,
        kw="china",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=1,
        crawl_only=True,
        discovered_urls=["https://example.com/nul-article"],
        mark_completed=True,
    )

    db = isolated_db()
    try:
        row = db.query(CrawledURL).filter(CrawledURL.id == url_id).first()
        kp_row = db.query(KeywordProgress).filter(
            KeywordProgress.search_query_id == 301, KeywordProgress.keyword == "china"
        ).first()
    finally:
        db.close()

    assert row.status == "matched", (
        "the row should have been written and matched normally, not left stuck "
        "pending/failed"
    )
    assert "\x00" not in row.full_content, "NUL byte must be stripped from full_content"
    assert row.full_content == "Beijing said  today that..."
    assert "\x00" not in row.title, "NUL byte must be stripped from title too"
    assert row.title == "China news"
    assert kp_row.status == "completed", (
        "the keyword itself must finish 'completed', not be dragged down by "
        "content that used to crash the write"
    )


def test_one_bad_row_write_failure_does_not_abort_the_whole_keyword(monkeypatch, isolated_db):
    """Part 2 regression test: this is the actual 'one bad row shouldn't kill
    everything' test, deliberately using a DB error UNRELATED to NUL bytes (so
    it isn't accidentally passing only because of the Part 1 fix). Simulates
    db.flush() raising for exactly one URL out of three in the same batch -
    the other two must still get written/committed correctly, and the keyword
    must still finish 'completed' rather than 'failed'."""
    db = isolated_db()
    q = SearchQuery(id=302, keyword="test", status="processing")
    db.add(q)
    kp = KeywordProgress(search_query_id=302, keyword="test", status="pending")
    db.add(kp)
    urls = []
    for i in range(3):
        u = CrawledURL(search_id=302, url=f"https://example.com/article-{i}",
                        domain="example.com", status="pending")
        db.add(u)
        urls.append(u)
    db.commit()
    for u in urls:
        db.refresh(u)
    url_ids = [u.id for u in urls]
    db.close()

    poison_id = url_ids[1]

    def fake_crawl_url_task(url_id, **kwargs):
        return url_id, {"status": "matched", "full_content": f"content-{url_id}"}

    monkeypatch.setattr(qm, "crawl_url_task", fake_crawl_url_task)

    # Wrap the isolated_db sessionmaker so the ONE session _process_single_keyword
    # creates for itself has a flush() that raises for the poisoned URL's write
    # only - an unrelated exception, not a NUL-byte one - and behaves normally
    # for every other write.
    def poisoned_session_factory():
        session = isolated_db()
        real_flush = session.flush

        def flush_maybe_raise(*args, **kwargs):
            for obj in list(session.dirty) + list(session.new):
                if isinstance(obj, CrawledURL) and obj.id == poison_id:
                    raise RuntimeError(f"Simulated unrelated DB failure for url_id={obj.id}")
            return real_flush(*args, **kwargs)

        session.flush = flush_maybe_raise
        return session

    monkeypatch.setattr(qm, "SessionLocal", poisoned_session_factory)

    query = _direct_query_mock(direct_urls="\n".join(u.url for u in urls))

    # Must not raise, and must not mark the keyword "failed".
    qm._process_single_keyword(
        search_id=302,
        kw="test",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=1,
        crawl_only=True,
        discovered_urls=[u.url for u in urls],
        mark_completed=True,
    )

    db = isolated_db()
    try:
        rows = {r.id: r for r in db.query(CrawledURL).filter(CrawledURL.search_id == 302).all()}
        kp_row = db.query(KeywordProgress).filter(
            KeywordProgress.search_query_id == 302, KeywordProgress.keyword == "test"
        ).first()
    finally:
        db.close()

    for uid in url_ids:
        if uid == poison_id:
            assert rows[uid].status == "pending", (
                "the row whose write failed should be left as-is (still 'pending', "
                "to be swept up by process_search_query()'s end-of-job cleanup), not "
                "silently marked matched with data that was never actually committed"
            )
        else:
            assert rows[uid].status == "matched", (
                f"url_id={uid} should have been written normally despite the other "
                f"URL's write failure in the same batch"
            )
            assert rows[uid].full_content == f"content-{uid}"

    assert kp_row.status == "completed", (
        "one bad row's write failure must not propagate out of the result-processing "
        "loop and abort the whole keyword - see search_id=148 ('china'/'POJK' both "
        "marked 'failed' entirely because of exactly this)"
    )


# ── Gap 1 fix: re-test already-crawled content against a new keyword ─────────

def test_retest_already_crawled_row_against_new_keyword(monkeypatch, isolated_db):
    """Gap 1 regression test (search_id=148/152 - e.g.
    https://nagalandpost.com/pok-protests-intensify-amid-fears-of-direct-control/,
    which sat 'skipped' with matched_keywords=[] forever because no keyword
    whose OWN discovery later found the same URL ever got a chance to test it).

    Simulates a CrawledURL row already left 'skipped' (crawled once under some
    other keyword, matched_keywords=[]) whose STORED content would genuinely
    match a keyword ('kw2') that independently discovers the same URL later in
    the same job. Running the flow for kw2 must re-test it from the already-
    stored content - no re-fetch (crawl_url_task must not be called at all) -
    and:
      * the row transitions to status='matched'
      * 'kw2' is appended to matched_keywords
      * KeywordProgress.articles_found for kw2 increments by 1
      * SearchQuery.total_urls_matched increments by exactly 1 (first time
        this URL becomes matched)
      * total_urls_crawled is NOT incremented again (it was already crawled)

    Then a SECOND, different keyword ('kw3') that also independently
    discovers the same URL and ALSO genuinely matches the stored content is
    re-tested against the now-already-matched row. This must:
      * add 'kw3' to matched_keywords alongside 'kw2'
      * increment KeywordProgress.articles_found for kw3 by 1
      * NOT increment total_urls_matched again (same URL, already counted as
        matched once - matching a second keyword must not double-count it)
    """
    import json as _json

    db = isolated_db()
    q = SearchQuery(
        id=310, keyword='["china", "PoK protests", "direct control"]', status="processing",
        total_urls_matched=0, total_urls_crawled=1,
    )
    db.add(q)
    for kw_name in ("PoK protests", "direct control"):
        db.add(KeywordProgress(search_query_id=310, keyword=kw_name, status="pending", articles_found=0))
    row = CrawledURL(
        search_id=310,
        url="https://example.com/pok-protests-intensify-amid-fears-of-direct-control",
        domain="example.com",
        status="skipped",
        matched_keywords="[]",
        title="PoK protests intensify amid fears of direct control",
        description="",
        full_content="Residents report PoK protests continuing this week across several towns.",
        error_message="Duplicate page content detected.",
        is_duplicate=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    row_id = row.id
    db.close()

    def _explode_if_crawled(url_id, **kwargs):
        raise AssertionError(
            "crawl_url_task must NOT be called for a re-test of an "
            "already-crawled row - this must be a cheap, no-refetch re-test"
        )

    monkeypatch.setattr(qm, "crawl_url_task", _explode_if_crawled)

    query = _direct_query_mock(
        direct_urls=row.url,
        match_type="phrase",
        case_sensitive=False,
        exact_match=False,
    )

    # ── First re-test: kw2, a keyword whose own discovery independently
    # finds the already-crawled (and "skipped") URL, and genuinely matches
    # its stored title.
    qm._process_single_keyword(
        search_id=310,
        kw="PoK protests",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=3,
        crawl_only=True,
        discovered_urls=[row.url],
        mark_completed=True,
    )

    db = isolated_db()
    try:
        q_after = db.query(SearchQuery).filter(SearchQuery.id == 310).first()
        row_after = db.query(CrawledURL).filter(CrawledURL.id == row_id).first()
        kp2_after = db.query(KeywordProgress).filter(
            KeywordProgress.search_query_id == 310, KeywordProgress.keyword == "PoK protests"
        ).first()
    finally:
        db.close()

    assert row_after.status == "matched", "row must transition from skipped to matched"
    assert "PoK protests" in _json.loads(row_after.matched_keywords)
    assert kp2_after.articles_found == 1
    assert q_after.total_urls_matched == 1, "first keyword to match this URL must increment total_urls_matched by 1"
    assert q_after.total_urls_crawled == 1, "re-testing already-stored content must NOT bump total_urls_crawled again"

    # ── Second re-test: kw3, a DIFFERENT keyword whose own discovery also
    # independently finds the SAME URL (now already "matched" under kw2) and
    # ALSO genuinely matches the stored title. Must add kw3's own progress
    # credit without double-counting total_urls_matched for the same URL.
    qm._process_single_keyword(
        search_id=310,
        kw="direct control",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=3,
        crawl_only=True,
        discovered_urls=[row.url],
        mark_completed=True,
    )

    db = isolated_db()
    try:
        q_after2 = db.query(SearchQuery).filter(SearchQuery.id == 310).first()
        row_after2 = db.query(CrawledURL).filter(CrawledURL.id == row_id).first()
        kp3_after = db.query(KeywordProgress).filter(
            KeywordProgress.search_query_id == 310, KeywordProgress.keyword == "direct control"
        ).first()
    finally:
        db.close()

    assert row_after2.status == "matched"
    matched_list = _json.loads(row_after2.matched_keywords)
    assert "PoK protests" in matched_list and "direct control" in matched_list
    assert kp3_after.articles_found == 1, "kw3 genuinely matched and must get its own progress credit"
    assert q_after2.total_urls_matched == 1, (
        "the SAME URL matching a second keyword must NOT double-count "
        "total_urls_matched - it was already counted as matched once"
    )


# ── Concurrent-redundant-crawl merge fix (found while verifying Gap 1 live) ──

def test_concurrent_redundant_crawl_does_not_clobber_earlier_match(monkeypatch):
    """Regression test for a bug found while live-verifying the Gap 1 fix
    against search_id=158: two DIFFERENT keywords whose own candidate_urls
    BOTH independently contain the SAME URL can each end up submitting their
    OWN crawl_url_task for it if both keywords' Step 2 snapshot the row while
    it's still 'pending' (a real race - Step 2 only diverts to the no-refetch
    re-test path for a row that's ALREADY terminal at snapshot time; two
    keyword-worker threads can both see 'pending' before either finishes).
    Confirmed live: url_id 17079 in search_id=158 was fetched 3 separate
    times (once per keyword whose candidate list happened to include it), and
    although its title literally contained "PoK protests" verbatim - a match
    that needs neither Part 1 nor Part 2's fixes - the row's final
    matched_keywords ended up "[]" because a LATER, genuinely non-matching
    keyword's write silently overwrote an EARLIER keyword's real match.

    Simulates this by having the mocked crawl_url_task for kw2 (nokw2's own
    analysis result is a genuine non-match) commit a DIFFERENT keyword's
    (kw1) matching result to the SAME row, via a separate session, in the
    middle of kw2's own crawl_url_task call - exactly mirroring "another
    keyword's crawl_url_task already completed and committed while this one
    was still fetching/analyzing." The row must end up 'matched' with BOTH
    keywords' names present in matched_keywords, not just whichever call
    wrote last.
    """
    import json as _json
    from sqlalchemy.pool import StaticPool

    # sqlite:///:memory: is per-connection - the real ThreadPoolExecutor
    # worker thread that crawl_url_task runs on (see _process_single_keyword's
    # Step 3) would otherwise get its own empty database when it calls
    # SessionLocal(). StaticPool forces every thread to share the ONE actual
    # connection/database - same pattern as
    # test_crawl_concurrency_semaphore_caps_simultaneous_work above.
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(qm, "SessionLocal", TestSessionLocal)

    db = TestSessionLocal()
    q = SearchQuery(id=311, keyword='["kw1", "kw2"]', status="processing",
                     total_urls_matched=0, total_urls_crawled=0)
    db.add(q)
    db.add(KeywordProgress(search_query_id=311, keyword="kw2", status="pending", articles_found=0))
    row = CrawledURL(search_id=311, url="https://example.com/race-article",
                      domain="example.com", status="pending")
    db.add(row)
    db.commit()
    db.refresh(row)
    row_id = row.id
    db.close()

    def fake_crawl_url_task(url_id, keyword=None, **kwargs):
        # Simulate kw1's OWN crawl_url_task (a separate keyword-worker thread,
        # its own independent DB session) completing and committing its
        # genuine match WHILE this (kw2's) call is still "in flight" - i.e.
        # before THIS call's own write-back has happened.
        side_db = TestSessionLocal()
        try:
            side_row = side_db.query(CrawledURL).filter(CrawledURL.id == url_id).first()
            side_row.status = "matched"
            side_row.matched_keywords = _json.dumps(["kw1"])
            side_row.full_content = "kw1's own crawl content"
            side_db.commit()
        finally:
            side_db.close()

        # kw2's OWN analysis of the SAME page is a genuine non-match.
        return url_id, {"status": "skipped", "matched_keywords": "[]", "full_content": "kw2's own crawl content"}

    monkeypatch.setattr(qm, "crawl_url_task", fake_crawl_url_task)

    query = _direct_query_mock(direct_urls=row.url, match_type="phrase",
                                case_sensitive=False, exact_match=False)

    qm._process_single_keyword(
        search_id=311,
        kw="kw2",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=2,
        crawl_only=True,
        discovered_urls=[row.url],
        mark_completed=True,
    )

    db = TestSessionLocal()
    try:
        row_after = db.query(CrawledURL).filter(CrawledURL.id == row_id).first()
    finally:
        db.close()

    assert row_after.status == "matched", (
        "kw2's own non-match write must not regress the row back from "
        "'matched' (committed concurrently by kw1) to 'skipped'"
    )
    matched_list = _json.loads(row_after.matched_keywords)
    assert "kw1" in matched_list, "kw1's earlier genuine match must survive"


# ── Broad re-test: catches URLs outside the current keyword's OWN candidates ──

def test_retest_covers_terminal_rows_outside_current_keywords_own_candidates(monkeypatch, isolated_db):
    """Coordinator-requested extension: _retest_existing_row_for_keyword() (the
    Gap 1 fix) only fires from inside the candidate_urls-processing loop - it
    only re-tests a URL against `kw` if `kw`'s OWN discovery this call
    happened to independently surface that exact URL as one of ITS OWN
    candidates. That's too narrow: confirmed live in search_id=158, "PoK
    protests intensify amid fears of direct control" (title, verbatim) sat
    'skipped' with matched_keywords=[] and was never re-tested against the
    "PoK protests" keyword specifically because THAT keyword's own discovery
    never happened to surface that particular URL as a candidate - even
    though match_keyword_against_stored_content() correctly returns True for
    it directly.

    This proves _retest_all_terminal_rows_for_keyword() closes that gap: a
    row that's already terminal ('skipped') and whose URL is NOT anywhere in
    the current keyword's own discovered_urls/candidate_urls must still get
    picked up and correctly re-tested/matched, purely because it's an
    already-terminal row for this search_id.
    """
    import json as _json

    db = isolated_db()
    q = SearchQuery(id=312, keyword='["china", "PoK protests"]', status="processing",
                     total_urls_matched=0, total_urls_crawled=1)
    db.add(q)
    db.add(KeywordProgress(search_query_id=312, keyword="PoK protests", status="pending", articles_found=0))
    # This row was crawled under some OTHER keyword ("china") whose own
    # discovery found it; "PoK protests"'s own discovery never surfaces this
    # URL at all - it will NOT appear in discovered_urls below.
    never_discovered_by_kw2 = CrawledURL(
        search_id=312,
        url="https://nagalandpost.com/pok-protests-intensify-amid-fears-of-direct-control/",
        domain="nagalandpost.com",
        status="skipped",
        matched_keywords="[]",
        title="PoK protests intensify amid fears of direct control",
        description="",
        full_content="Residents report unrest continuing this week across several towns.",
    )
    db.add(never_discovered_by_kw2)
    db.commit()
    db.refresh(never_discovered_by_kw2)
    row_id = never_discovered_by_kw2.id
    db.close()

    def _explode_if_crawled(url_id, **kwargs):
        raise AssertionError("crawl_url_task must NOT be called for this re-test")

    monkeypatch.setattr(qm, "crawl_url_task", _explode_if_crawled)

    query = _direct_query_mock(
        direct_urls="https://example.com/something-unrelated",
        match_type="phrase", case_sensitive=False, exact_match=False,
    )

    # Note: discovered_urls deliberately does NOT include
    # never_discovered_by_kw2.url - "PoK protests"'s own discovery this call
    # only found a completely different, unrelated URL.
    qm._process_single_keyword(
        search_id=312,
        kw="PoK protests",
        query=query,
        languages_filter=None,
        seen_content_hashes=set(),
        seen_simhashes=[],
        seen_lock=threading.Lock(),
        total_keyword_count=2,
        crawl_only=True,
        discovered_urls=["https://example.com/something-unrelated"],
        mark_completed=True,
    )

    db = isolated_db()
    try:
        row_after = db.query(CrawledURL).filter(CrawledURL.id == row_id).first()
        q_after = db.query(SearchQuery).filter(SearchQuery.id == 312).first()
        kp_after = db.query(KeywordProgress).filter(
            KeywordProgress.search_query_id == 312, KeywordProgress.keyword == "PoK protests"
        ).first()
    finally:
        db.close()

    assert row_after.status == "matched", (
        "a terminal row outside this keyword's own candidate_urls must still "
        "be picked up by the broad re-test pass"
    )
    assert "PoK protests" in _json.loads(row_after.matched_keywords)
    assert kp_after.articles_found == 1
    assert q_after.total_urls_matched == 1
    assert q_after.total_urls_crawled == 1, "re-testing stored content must not bump total_urls_crawled again"
