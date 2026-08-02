"""
backend/discovery/conftest.py — shared pytest fixtures for the discovery test suite.

Why this exists
----------------
Every strategy module in this package guards its use of crawl4ai behind a
module-level `CRAWL4AI_AVAILABLE` boolean (see sitemap.py, deep_crawl.py,
news_sitemap.py). All 74 tests in this directory were originally written and
verified on a machine WITHOUT crawl4ai installed, so `CRAWL4AI_AVAILABLE` was
always False there and every test's mocks (which monkeypatch `requests`/
`requests.Session`) exercised the documented, empirically-verified pure
requests+lxml fallback path -- the ONLY path this layer has ever actually run.

Once crawl4ai is installed in the environment running these tests,
`CRAWL4AI_AVAILABLE` flips to True and `discover()` tries the crawl4ai path
FIRST. That path uses crawl4ai's own internal HTTP/browser client, which is
completely separate from `requests` -- so none of the existing `requests`
mocks intercept it, and crawl4ai instead makes REAL live network requests
(confirmed via crawl4ai's own `[FETCH]`/`[SCRAPE]`/`[COMPLETE]` stderr
logging hitting real sites such as fmprc.gov.cn during a real test run).
This repo has an explicit rule against live-network tests -- see CLAUDE.md
and backend/discovery/README.md ("Never add live-network tests... one IP ban
already occurred during profiling") -- and a global rate-limit/hang risk:
crawl4ai's AsyncUrlSeeder/AsyncWebCrawler calls block on real DNS/TLS/HTTP
against arbitrary hostnames used only as test fixtures (e.g.
"example-deep.test"), which is what caused test_news_sitemap.py to hang.

Fix: force CRAWL4AI_AVAILABLE False for every test in this directory by
default, regardless of whether the package happens to be installed in
whatever environment runs the suite. This makes the tests deterministic and
guarantees no test can ever make a real network call through crawl4ai. Tests
that specifically want to exercise the crawl4ai-available code path (e.g. to
verify it's at least reachable/doesn't crash on construction) should
monkeypatch the flag back to True themselves -- pytest's `monkeypatch`
fixture is function-scoped and shared with this autouse fixture, so a
per-test `monkeypatch.setattr(..., "CRAWL4AI_AVAILABLE", True)` cleanly
overrides this default for the duration of that one test and is reverted
automatically afterward, alongside mocking crawl4ai's own entry points
(AsyncWebCrawler, AsyncUrlSeeder) so no real network call can occur even then.

news_sitemap.py imports CRAWL4AI_AVAILABLE by name from sitemap.py
(`from .sitemap import CRAWL4AI_AVAILABLE`), which creates its own independent
module-level binding -- patching backend.discovery.sitemap.CRAWL4AI_AVAILABLE
does NOT affect backend.discovery.news_sitemap.CRAWL4AI_AVAILABLE, so each
module's copy must be patched at its own point of use.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def force_crawl4ai_unavailable(monkeypatch):
    """Default every test in backend/discovery/ to the documented, verified
    crawl4ai-absent fallback path, independent of whether crawl4ai is actually
    installed in the environment running the suite."""
    import backend.discovery.sitemap as sitemap_module
    import backend.discovery.deep_crawl as deep_crawl_module
    import backend.discovery.news_sitemap as news_sitemap_module

    monkeypatch.setattr(sitemap_module, "CRAWL4AI_AVAILABLE", False)
    monkeypatch.setattr(deep_crawl_module, "CRAWL4AI_AVAILABLE", False)
    monkeypatch.setattr(news_sitemap_module, "CRAWL4AI_AVAILABLE", False)
    yield
