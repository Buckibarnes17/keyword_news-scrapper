"""
Unit tests for backend/discovery/oai_pmh.py.

All HTTP is mocked -- no live network. These tests cover the gotchas called out in
the implementation task: resumptionToken continuation, the 0x1E control-character
XML recovery, deleted-record (tombstone) skipping, and max_urls truncation.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.discovery.oai_pmh import OAIPMHStrategy  # noqa: E402


def make_profile(**overrides):
    profile = {
        "domain": "nepjol.info",
        "seed_url": "https://nepjol.info/",
        "enabled": True,
        "strategy": "oai_pmh",
        "robots": {"respect": True, "crawl_delay": 0.0},
        "transport": {"timeout_s": 5, "max_retries": 0},
        "oai_pmh": {"endpoint": "https://nepjol.info/index.php/index/oai?verb=ListRecords&metadataPrefix=oai_dc"},
    }
    profile.update(overrides)
    return profile


def record_xml(identifier_url, title="Sample Title", description="Sample description",
                date="2026-07-01", deleted=False, extra_identifier=None):
    if deleted:
        return f'''
        <record>
          <header status="deleted">
            <identifier>oai:nepjol.info:article/{identifier_url}</identifier>
            <datestamp>{date}</datestamp>
          </header>
        </record>'''
    extra = f"<dc:identifier>{extra_identifier}</dc:identifier>" if extra_identifier else ""
    return f'''
        <record>
          <header>
            <identifier>oai:nepjol.info:article/{identifier_url}</identifier>
            <datestamp>{date}</datestamp>
          </header>
          <metadata>
            <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                       xmlns:dc="http://purl.org/dc/elements/1.1/">
              <dc:title>{title}</dc:title>
              <dc:creator>Jane Doe</dc:creator>
              <dc:date>{date}</dc:date>
              <dc:identifier>{identifier_url}</dc:identifier>
              {extra}
              <dc:description>{description}</dc:description>
            </oai_dc:dc>
          </metadata>
        </record>'''


def list_records_xml(records_xml, resumption_token=None):
    token_el = f"<resumptionToken>{resumption_token}</resumptionToken>" if resumption_token else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        {records_xml}
        {token_el}
      </ListRecords>
    </OAI-PMH>'''.encode("utf-8")


def _resp(status_code=200, content=b"", ok=None):
    r = MagicMock()
    r.status_code = status_code
    r.ok = ok if ok is not None else (200 <= status_code < 400)
    r.content = content
    return r


def _allow_robots():
    r = MagicMock()
    r.status_code = 200
    r.ok = True
    r.text = "User-agent: *\nAllow: /\n"
    return r


def test_single_page_no_resumption_token():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    xml = list_records_xml(
        record_xml("https://nepjol.info/index.php/JournalX/article/view/123")
    )
    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover()

    assert len(result.urls) == 1
    assert result.urls[0].url == "https://nepjol.info/index.php/JournalX/article/view/123"
    assert result.urls[0].title == "Sample Title"
    assert result.urls[0].is_article is True
    assert result.errors == []


def test_resumption_token_continuation():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    page1 = list_records_xml(
        record_xml("https://nepjol.info/index.php/JournalX/article/view/1"),
        resumption_token="TOKEN123",
    )
    page2 = list_records_xml(
        record_xml("https://nepjol.info/index.php/JournalX/article/view/2"),
    )

    captured_params = []

    def fake_get(self, url, params=None, headers=None, timeout=None):
        if "robots.txt" in url:
            return _allow_robots()
        captured_params.append(dict(params or {}))
        if len(captured_params) == 1:
            return _resp(200, page1)
        return _resp(200, page2)

    with patch("requests.Session.get", new=fake_get):
        result = strat.discover()

    assert len(result.urls) == 2
    urls = {u.url for u in result.urls}
    assert "https://nepjol.info/index.php/JournalX/article/view/1" in urls
    assert "https://nepjol.info/index.php/JournalX/article/view/2" in urls

    # Second request must use ONLY verb+resumptionToken -- no metadataPrefix.
    second_call_params = captured_params[1]
    assert second_call_params.get("resumptionToken") == "TOKEN123"
    assert "metadataPrefix" not in second_call_params


def test_control_char_0x1e_recovered():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    good_record = record_xml("https://nepjol.info/index.php/JournalX/article/view/9")
    # Inject a raw 0x1E control char into the description, which a strict XML
    # parser rejects with "PCDATA invalid Char value 30".
    xml = list_records_xml(good_record)
    xml = xml.replace(b"Sample description", b"Sample\x1edescription")

    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover()  # must not raise despite the control char

    assert len(result.urls) == 1
    assert result.errors == []


def test_deleted_record_skipped():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    records = (
        record_xml("https://nepjol.info/index.php/JournalX/article/view/1")
        + record_xml("tombstone", deleted=True)
        + record_xml("https://nepjol.info/index.php/JournalX/article/view/2")
    )
    xml = list_records_xml(records)

    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover()

    assert len(result.urls) == 2  # the deleted one is excluded


def test_max_urls_truncation():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    records = "".join(
        record_xml(f"https://nepjol.info/index.php/JournalX/article/view/{i}")
        for i in range(1, 6)
    )
    xml = list_records_xml(records)

    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover(max_urls=3)

    assert len(result.urls) == 3
    assert result.truncated is True


def test_since_maps_to_from_param():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    xml = list_records_xml(
        record_xml("https://nepjol.info/index.php/JournalX/article/view/1")
    )
    captured = {}

    def fake_get(self, url, params=None, headers=None, timeout=None):
        if "robots.txt" in url:
            return _allow_robots()
        captured["params"] = params
        return _resp(200, xml)

    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with patch("requests.Session.get", new=fake_get):
        strat.discover(since=since)

    assert captured["params"].get("from") == "2026-07-01"


def test_keyword_filtered_client_side():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    records = (
        record_xml("https://nepjol.info/index.php/JournalX/article/view/1",
                    title="About Nepal Agriculture")
        + record_xml("https://nepjol.info/index.php/JournalX/article/view/2",
                      title="Unrelated Topic", description="nothing relevant here")
    )
    xml = list_records_xml(records)

    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover(keyword="Nepal")

    assert len(result.urls) == 1
    assert "Nepal" in result.urls[0].title


def test_prefers_http_identifier_over_doi():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    xml = list_records_xml(
        record_xml("https://nepjol.info/index.php/JournalX/article/view/1",
                    extra_identifier="10.3126/example.doi")
    )
    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover()

    assert len(result.urls) == 1
    assert result.urls[0].url.startswith("https://")


def test_discover_never_raises_on_network_exception():
    import requests

    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    with patch("requests.Session.get", side_effect=requests.ConnectionError("boom")):
        result = strat.discover()  # must not raise

    assert result.urls == []
    assert len(result.errors) >= 1


def test_no_records_match_is_not_an_error():
    profile = make_profile()
    strat = OAIPMHStrategy(profile)

    xml = b'''<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <error code="noRecordsMatch">No records match</error>
    </OAI-PMH>'''

    with patch("requests.Session.get", side_effect=[_allow_robots(), _resp(200, xml)]):
        result = strat.discover()

    assert result.urls == []
    assert result.errors == []
