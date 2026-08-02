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
