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
        lambda urls, query, keyword=None: (
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
        lambda urls, query, keyword=None: (
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

    def fake_run_direct_discovery(urls, query, keyword=None):
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
                         lambda urls, query, keyword=None: ({"https://example.com/": []}, {"example.com"}))

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
                         lambda urls, query, keyword=None: ({"https://example.com/": []}, {"example.com"}))

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
