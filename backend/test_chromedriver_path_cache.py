"""
backend/test_chromedriver_path_cache.py — regression test for the
webdriver-manager lock-contention bug.

Root cause (confirmed via real pilot fetch/analyze timing instrumentation,
see commit 319cf3b's message and the follow-up fix): queue_manager.py's
crawl_url_task() creates a brand-new Crawler() instance per URL task, and the
old Crawler._get_selenium_driver() called ChromeDriverManager().install()
inline, guarded only by a *per-instance* threading.Lock (self._driver_lock).
That per-instance lock does nothing to stop many concurrent Crawler
instances from all calling ChromeDriverManager().install() at once - they all
serialize on webdriver-manager's own filesystem lock
(~/.wdm/.wdm-lock-chromedriver-linux64) instead, which was observed pushing
individual fetches past the pipeline's 120s watchdog.

The fix resolves the chromedriver path once per process via a module-level
cache (_CHROMEDRIVER_PATH) guarded by a module-level lock
(_CHROMEDRIVER_PATH_LOCK), so ChromeDriverManager().install() is called AT
MOST ONCE regardless of how many threads or Crawler instances need a driver.

This test proves that specifically: 20 threads calling
backend.crawler._get_chromedriver_path() concurrently must result in exactly
one call to ChromeDriverManager().install(), not one per thread.

Run with: python3 -m pytest backend/test_chromedriver_path_cache.py -v
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch, MagicMock

import backend.crawler as crawler_mod


def _reset_cache():
    """The cache is process-global; tests must not leak state between them."""
    crawler_mod._CHROMEDRIVER_PATH = None


def test_concurrent_callers_install_at_most_once():
    """20 concurrent threads calling _get_chromedriver_path() must trigger
    ChromeDriverManager().install() at most once, not once per thread."""
    _reset_cache()

    install_calls = []
    install_lock = threading.Lock()

    def fake_install(self):
        # Simulate the real filesystem-lock-bound work taking measurable time,
        # so that without the fix, concurrent callers would genuinely overlap
        # and each acquire+call independently rather than the race being too
        # fast to observe.
        with install_lock:
            install_calls.append(1)
        time.sleep(0.05)
        return "/fake/path/to/chromedriver"

    with patch.object(crawler_mod, "patch_chromedriver_if_needed", return_value=None), \
         patch.object(crawler_mod.ChromeDriverManager, "install", fake_install):

        results = []
        results_lock = threading.Lock()

        def worker():
            path = crawler_mod._get_chromedriver_path()
            with results_lock:
                results.append(path)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 20, "not all threads completed"
        assert all(p == "/fake/path/to/chromedriver" for p in results)
        assert len(install_calls) == 1, (
            f"expected ChromeDriverManager().install() to be called exactly once "
            f"across 20 concurrent callers, got {len(install_calls)}"
        )

    _reset_cache()


def test_cached_path_returned_without_reinstalling():
    """Once resolved, subsequent calls must hit the in-memory cache and never
    touch ChromeDriverManager().install() again."""
    _reset_cache()

    with patch.object(crawler_mod, "patch_chromedriver_if_needed", return_value=None), \
         patch.object(crawler_mod.ChromeDriverManager, "install", return_value="/fake/driver") as mock_install:

        first = crawler_mod._get_chromedriver_path()
        second = crawler_mod._get_chromedriver_path()
        third = crawler_mod._get_chromedriver_path()

        assert first == second == third == "/fake/driver"
        assert mock_install.call_count == 1

    _reset_cache()


def test_failed_resolution_is_not_cached_and_can_be_retried():
    """A transient failure (e.g. network error downloading the driver) must
    not poison the cache - a later call should retry, not raise forever."""
    _reset_cache()

    with patch.object(crawler_mod, "patch_chromedriver_if_needed", return_value=None), \
         patch.object(
             crawler_mod.ChromeDriverManager,
             "install",
             side_effect=[RuntimeError("simulated transient network failure"), "/fake/driver-after-retry"],
         ) as mock_install:

        try:
            crawler_mod._get_chromedriver_path()
            assert False, "expected the first call to raise"
        except RuntimeError:
            pass

        # Cache must still be empty after the failure.
        assert crawler_mod._CHROMEDRIVER_PATH is None

        # A subsequent call must retry (not raise a cached failure) and succeed.
        path = crawler_mod._get_chromedriver_path()
        assert path == "/fake/driver-after-retry"
        assert mock_install.call_count == 2

    _reset_cache()


def test_get_selenium_driver_uses_module_level_cache():
    """Crawler._get_selenium_driver() must resolve its driver path via the
    module-level _get_chromedriver_path() cache rather than calling
    ChromeDriverManager().install() inline, so two Crawler instances share
    one resolution."""
    _reset_cache()

    fake_driver = MagicMock()
    fake_driver.execute_cdp_cmd = MagicMock()

    with patch.object(crawler_mod, "patch_chromedriver_if_needed", return_value=None), \
         patch.object(crawler_mod.ChromeDriverManager, "install", return_value="/fake/driver") as mock_install, \
         patch.object(crawler_mod, "Service", return_value=MagicMock()), \
         patch.object(crawler_mod.webdriver, "Chrome", return_value=fake_driver):

        c1 = crawler_mod.Crawler()
        c2 = crawler_mod.Crawler()

        c1._get_selenium_driver()
        c2._get_selenium_driver()

        assert mock_install.call_count == 1, (
            f"two separate Crawler instances resolving a driver should share one "
            f"install() call, got {mock_install.call_count}"
        )

    _reset_cache()
