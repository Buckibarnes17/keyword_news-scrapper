"""
Unit tests for backend/discovery/wp_api.py.

All HTTP is mocked -- no live network. These tests cover the gotchas called out in
the implementation task: X-WP-Total/X-WP-TotalPages driven pagination, HTTP 400 as a
normal end-of-pagination signal, a 403 site (mmbiztoday.com), and max_urls
truncation.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.discovery.wp_api import WordPressAPIStrategy  # noqa: E402


def make_profile(**overrides):
    profile = {
        "domain": "example.com",
        "seed_url": "https://example.com/",
        "enabled": True,
        "strategy": "wp_api",
        "robots": {"respect": True, "crawl_delay": 0.0},
        "transport": {"timeout_s": 5, "max_retries": 0},
    }
    profile.update(overrides)
    return profile


def make_post(post_id, title="Hello &amp; World", link=None, date_gmt="2026-07-01T12:00:00"):
    return {
        "id": post_id,
        "link": link or f"https://example.com/{post_id}/",
        "title": {"rendered": title},
        "date_gmt": date_gmt,
        "excerpt": {"rendered": "an excerpt"},
        "author": 1,
        "_links": {},
    }


def _resp(status_code=200, json_data=None, headers=None, ok=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.ok = ok if ok is not None else (200 <= status_code < 400)
    r.headers = headers or {}
    r.text = text
    if json_data is not None:
        r.json.return_value = json_data
    return r


def _allow_robots():
    """robots.txt fetch returns a permissive file for _robots_allow()."""
    return _resp(200, headers={}, text="User-agent: *\nAllow: /\n")


def test_pagination_via_xwp_total_header():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    page1 = [make_post(i) for i in range(1, 101)]
    page2 = [make_post(i) for i in range(101, 151)]

    robots_resp = _allow_robots()
    resp1 = _resp(200, page1, headers={"X-WP-Total": "150", "X-WP-TotalPages": "2"})
    resp2 = _resp(200, page2, headers={"X-WP-Total": "150", "X-WP-TotalPages": "2"})

    with patch("requests.Session.get", side_effect=[robots_resp, resp1, resp2]):
        result = strat.discover(max_urls=1000)

    assert len(result.urls) == 150
    assert result.errors == []
    assert not result.truncated


def test_http_400_is_end_of_pagination_not_error():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    page1 = [make_post(i) for i in range(1, 101)]
    robots_resp = _allow_robots()
    resp1 = _resp(200, page1, headers={"X-WP-Total": "100", "X-WP-TotalPages": "1"})
    # Even if TotalPages logic didn't stop us, a stray page-2 request must be
    # treated as a clean stop, not an error.
    resp2 = _resp(400, ok=False)

    with patch("requests.Session.get", side_effect=[robots_resp, resp1, resp2]):
        result = strat.discover(max_urls=1000)

    assert len(result.urls) == 100
    assert result.errors == []


def test_403_site_returns_empty_and_logs_error_no_raise():
    """Simulates mmbiztoday.com: worked at profile time, 403s at runtime."""
    profile = make_profile(domain="mmbiztoday.com", seed_url="https://mmbiztoday.com/")
    strat = WordPressAPIStrategy(profile)

    robots_resp = _allow_robots()
    resp_403 = _resp(403, ok=False)

    with patch("requests.Session.get", side_effect=[robots_resp, resp_403]):
        result = strat.discover()

    assert result.urls == []
    assert any("403" in e for e in result.errors)


def test_max_urls_truncation():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    page1 = [make_post(i) for i in range(1, 101)]
    robots_resp = _allow_robots()
    resp1 = _resp(200, page1, headers={"X-WP-Total": "500", "X-WP-TotalPages": "5"})

    with patch("requests.Session.get", side_effect=[robots_resp, resp1]):
        result = strat.discover(max_urls=10)

    assert len(result.urls) == 10
    assert result.truncated is True


def test_keyword_pushed_server_side():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    robots_resp = _allow_robots()
    resp1 = _resp(200, [make_post(1)], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    captured = {}

    def fake_get(self, url, params=None, headers=None, timeout=None):
        if "robots.txt" in url:
            return robots_resp
        captured["params"] = params
        return resp1

    with patch("requests.Session.get", new=fake_get):
        strat.discover(keyword="Myanmar")

    assert captured["params"].get("search") == "Myanmar"


def test_title_html_entities_unescaped():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    robots_resp = _allow_robots()
    resp1 = _resp(200, [make_post(1, title="Cats &amp; Dogs")],
                   headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    with patch("requests.Session.get", side_effect=[robots_resp, resp1]):
        result = strat.discover()

    assert result.urls[0].title == "Cats & Dogs"


def test_published_at_is_timezone_aware_utc():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    robots_resp = _allow_robots()
    resp1 = _resp(200, [make_post(1, date_gmt="2026-07-01T12:00:00")],
                   headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    with patch("requests.Session.get", side_effect=[robots_resp, resp1]):
        result = strat.discover()

    dt = result.urls[0].published_at
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc) == datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_missing_fields_do_not_crash():
    """Every dict access must use .get() -- feed a post missing everything but link."""
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    sparse_post = {"link": "https://example.com/sparse/"}
    robots_resp = _allow_robots()
    resp1 = _resp(200, [sparse_post], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    with patch("requests.Session.get", side_effect=[robots_resp, resp1]):
        result = strat.discover()

    assert len(result.urls) == 1
    assert result.urls[0].title is None
    assert result.urls[0].published_at is None
    assert result.errors == []


def test_discover_never_raises_on_network_exception():
    import requests

    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    with patch("requests.Session.get", side_effect=requests.ConnectionError("boom")):
        result = strat.discover()  # must not raise

    assert result.urls == []
    assert len(result.errors) >= 1


def test_robots_override_skips_robots_check():
    """northeastlivetv.com / newslivetv.com: respect=False operator override means
    the robots.txt fetch must not even happen."""
    profile = make_profile(
        domain="northeastlivetv.com",
        seed_url="https://northeastlivetv.com/",
        robots={"respect": False, "crawl_delay": 0.0,
                "override_reason": "Operator decision 2026-08-01"},
    )
    strat = WordPressAPIStrategy(profile)
    assert strat.respects_robots() is False

    resp1 = _resp(200, [make_post(1, link="https://northeastlivetv.com/1/")],
                   headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    with patch("requests.Session.get", side_effect=[resp1]) as mock_get:
        result = strat.discover()

    assert len(result.urls) == 1
    # only one call was made (the posts fetch) -- robots.txt was never requested
    assert mock_get.call_count == 1


def test_include_pages_flag_is_opt_in():
    profile = make_profile(wp_api={"include_pages": True})
    strat = WordPressAPIStrategy(profile)

    robots_resp = _allow_robots()
    posts_resp = _resp(200, [make_post(1)], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})
    pages_resp = _resp(200, [make_post(2, link="https://example.com/2/")],
                        headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    with patch("requests.Session.get", side_effect=[robots_resp, posts_resp, pages_resp]):
        result = strat.discover()

    assert len(result.urls) == 2


def test_include_pages_defaults_off():
    profile = make_profile()
    strat = WordPressAPIStrategy(profile)

    robots_resp = _allow_robots()
    posts_resp = _resp(200, [make_post(1)], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})

    with patch("requests.Session.get", side_effect=[robots_resp, posts_resp]) as mock_get:
        result = strat.discover()

    assert len(result.urls) == 1
    # posts call + robots.txt call only, no pages call
    assert mock_get.call_count == 2
