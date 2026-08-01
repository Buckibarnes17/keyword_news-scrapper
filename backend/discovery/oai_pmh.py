"""
backend/discovery/oai_pmh.py — OAI-PMH discovery strategy.

Used by exactly one site in config/site_profiles.json: nepjol.info (Nepal Journals
Online), an Open Journal Systems (OJS) academic platform. nepjol's HTML search is
robots-disallowed (`Disallow: /*/search`), so OAI-PMH is not just the cheapest route
here, it is the only compliant one for bulk discovery.

VERIFIED (live, this session) facts this module is built on:
  - `https://nepjol.info/index.php/index/oai?verb=ListRecords&metadataPrefix=oai_dc`
    returns `completeListSize="61830"`.
  - 100 records per response; continue via <resumptionToken>. The continuation
    request is `?verb=ListRecords&resumptionToken={token}` with NO other params --
    adding metadataPrefix (or anything else) alongside a token is a protocol error
    per the OAI-PMH spec and was observed to be rejected.
  - Incremental harvest works via `&from=YYYY-MM-DD` (verified: from=2026-07-01 ->
    1,613 records). We map the `since` parameter onto this, making full re-harvests
    unnecessary for delta runs.
  - PARSER GOTCHA, verified: the XML contains raw 0x1E control characters that break
    a default lxml.etree.fromstring() ("PCDATA invalid Char value 30"). We MUST parse
    with etree.XMLParser(recover=True, huge_tree=True).
  - <header status="deleted"> tombstone records carry no <metadata> and are skipped.
  - oai_dc fields used: dc:title, dc:identifier (the article URL -- there may be
    several, including DOIs; we prefer the first http(s) one), dc:date, dc:creator,
    dc:description.
  - OAI-PMH has no server-side keyword search. When `keyword` is given we filter
    CLIENT-SIDE over dc:title + dc:description after harvesting -- this is NOT a
    server-side filter and is much less efficient than wp_api's ?search=; it exists
    only because OAI offers nothing better here.
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

from .base import DiscoveredURL, DiscoveryResult, DiscoveryStrategy, register

log = logging.getLogger("keywordscout.discovery.oai_pmh")

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
NSMAP = {"oai": OAI_NS, "dc": DC_NS}

DEFAULT_ENDPOINT_PATH = "/index.php/index/oai"
METADATA_PREFIX = "oai_dc"


@register
class OAIPMHStrategy(DiscoveryStrategy):
    """Discovers content via an OAI-PMH ListRecords harvest (oai_dc metadata)."""

    name = "oai_pmh"

    def discover(self, keyword: Optional[str] = None, max_urls: int = 500,
                 since: Optional[datetime] = None) -> DiscoveryResult:
        result = DiscoveryResult(strategy=self.name, domain=self.domain)

        endpoint = self._endpoint()
        session = requests.Session()

        # No operator override is recorded for nepjol.info, so respects_robots() is
        # True here; we still honour it generically per the base-strategy contract
        # rather than hardcoding either behaviour.
        if self.respects_robots() and not self._robots_allow(session, endpoint):
            result.errors.append(
                f"{self.domain}: robots.txt disallows the OAI endpoint path; "
                f"skipping (no operator override on file)")
            return result

        params: Dict[str, Any] = {"verb": "ListRecords", "metadataPrefix": METADATA_PREFIX}
        if since is not None:
            params["from"] = self._to_oai_date(since)

        resumption_token: Optional[str] = None
        first_request = True

        while True:
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                result.truncated = True
                break

            request_params = ({"verb": "ListRecords", "resumptionToken": resumption_token}
                               if resumption_token else params)

            try:
                resp = self._get_with_retries(session, endpoint, request_params)
            except Exception as exc:  # discover() MUST NOT raise — see base.DiscoveryStrategy
                # Deliberately broad: _get_with_retries re-raises the last exception, and a
                # non-requests error (bad profile value, lxml edge case, date parse) must not
                # take down discovery for the entire site.
                result.errors.append(
                    f"{self.domain}: error during OAI harvest: {exc!r}")
                break

            if resp is None:
                break

            if not resp.ok:
                result.errors.append(
                    f"{self.domain}: OAI endpoint returned HTTP {resp.status_code}")
                break

            try:
                root = self._parse_xml(resp.content)
            except Exception as exc:
                result.errors.append(f"{self.domain}: failed to parse OAI XML: {exc!r}")
                break

            if root is None:
                result.errors.append(f"{self.domain}: OAI response produced no XML tree")
                break

            oai_error = root.find("oai:error", NSMAP)
            if oai_error is not None:
                code = oai_error.get("code", "unknown")
                if code != "noRecordsMatch":
                    result.errors.append(
                        f"{self.domain}: OAI-PMH error code={code!r} text={oai_error.text!r}")
                break

            list_records = root.find("oai:ListRecords", NSMAP)
            if list_records is None:
                if first_request:
                    result.errors.append(
                        f"{self.domain}: OAI response missing ListRecords element")
                break

            records = list_records.findall("oai:record", NSMAP)
            hit_cap = False
            for idx, record in enumerate(records):
                du = self._record_to_discovered_url(record, keyword)
                if du is not None:
                    result.urls.append(du)
                if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                    hit_cap = True
                    if idx < len(records) - 1:
                        # More records were sitting unprocessed in this same page.
                        result.truncated = True
                    break

            token_el = list_records.find("oai:resumptionToken", NSMAP)
            token_text = (token_el.text or "").strip() if token_el is not None else ""

            if hit_cap:
                if token_text:
                    # A resumptionToken means the source has more to give even
                    # though we consumed everything in this page.
                    result.truncated = True
                break

            if not token_text:
                break

            resumption_token = token_text
            first_request = False
            time.sleep(self.crawl_delay())

        result.urls = self._cap(result.urls, max_urls, result)
        return result

    # ── internals ───────────────────────────────────────────────────────────

    def _endpoint(self) -> str:
        oai_cfg = self.profile.get("oai_pmh") or {}
        configured = oai_cfg.get("endpoint")
        if configured and isinstance(configured, str):
            # Strip any query string; we build params ourselves.
            return configured.split("?", 1)[0]
        base = (self.seed_url or f"https://{self.domain}/").rstrip("/")
        return base + DEFAULT_ENDPOINT_PATH

    def _robots_allow(self, session: requests.Session, endpoint: str) -> bool:
        """Best-effort robots.txt check for the OAI endpoint path.

        Failure to fetch/parse robots.txt is treated as "allowed" (fail-open) so a
        network hiccup never silently blocks the only compliant discovery route for
        this site.
        """
        parsed = urlparse(endpoint)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            resp = session.get(robots_url, headers=self.headers(), timeout=self.timeout())
            if not resp.ok:
                return True
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(resp.text.splitlines())
            ua = self.headers().get("User-Agent", "*")
            return rp.can_fetch(ua, endpoint)
        except Exception as exc:
            log.warning("%s: robots.txt check failed (%r); assuming allowed",
                        self.domain, exc)
            return True

    def _get_with_retries(self, session: requests.Session, endpoint: str,
                           params: Dict[str, Any]) -> Optional[requests.Response]:
        last_exc: Optional[Exception] = None
        attempts = max(1, self.max_retries() + 1)
        for attempt in range(attempts):
            try:
                return session.get(endpoint, params=params, headers=self.headers(),
                                    timeout=self.timeout())
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s: attempt %d/%d failed for %s: %r",
                            self.domain, attempt + 1, attempts, endpoint, exc)
                if attempt < attempts - 1:
                    time.sleep(self.crawl_delay())
        if last_exc is not None:
            raise last_exc
        return None

    @staticmethod
    def _parse_xml(content: bytes) -> Optional[etree._Element]:
        # VERIFIED: nepjol's OAI XML contains raw 0x1E control characters that a
        # default parser rejects ("PCDATA invalid Char value 30"). recover=True
        # tolerates that; huge_tree=True accommodates the large ListRecords payload.
        parser = etree.XMLParser(recover=True, huge_tree=True)
        root = etree.fromstring(content, parser=parser)
        return root

    def _record_to_discovered_url(self, record: etree._Element,
                                   keyword: Optional[str]) -> Optional[DiscoveredURL]:
        header = record.find("oai:header", NSMAP)
        if header is not None and header.get("status") == "deleted":
            # Tombstone record -- no metadata, skip per spec.
            return None

        metadata_el = record.find("oai:metadata", NSMAP)
        if metadata_el is None:
            return None

        dc_el = metadata_el.find("oai_dc:dc", {"oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/"})
        if dc_el is None:
            # Fall back to scanning any descendant with dc: children (namespace
            # prefixes on records in the wild are not always exactly oai_dc:dc).
            dc_el = metadata_el

        title = self._first_text(dc_el, "dc:title")
        description = self._first_text(dc_el, "dc:description")
        creator = self._first_text(dc_el, "dc:creator")
        date_text = self._first_text(dc_el, "dc:date")

        url = self._pick_identifier_url(dc_el)
        if url is None:
            return None
        if not self.host_allowed(url):
            return None

        if keyword:
            # NOT server-side: OAI-PMH has no keyword search, so we filter
            # client-side over the fields we already harvested.
            haystack = " ".join(t for t in (title, description) if t).lower()
            if keyword.lower() not in haystack:
                return None

        published_at = self._parse_dc_date(date_text)
        is_article = self.classify_url(url)
        if is_article is None:
            # An OAI record is inherently a document/article-level unit.
            is_article = True

        metadata: Dict[str, Any] = {"creator": creator, "description": description}

        try:
            return DiscoveredURL(
                url=url,
                source=self.name,
                title=title,
                published_at=published_at,
                is_article=is_article,
                metadata=metadata,
            )
        except ValueError as exc:
            log.warning("%s: skipping malformed OAI identifier %r: %r",
                        self.domain, url, exc)
            return None

    @staticmethod
    def _first_text(el: etree._Element, tag: str) -> Optional[str]:
        found = el.find(tag, NSMAP)
        if found is None or found.text is None:
            return None
        text = found.text.strip()
        return text or None

    @staticmethod
    def _pick_identifier_url(dc_el: etree._Element) -> Optional[str]:
        identifiers = dc_el.findall("dc:identifier", NSMAP)
        candidates = [(i.text or "").strip() for i in identifiers if i is not None]
        candidates = [c for c in candidates if c]
        for c in candidates:
            if c.startswith("http://") or c.startswith("https://"):
                return c
        return None

    @staticmethod
    def _parse_dc_date(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        # dc:date in OJS oai_dc is typically YYYY-MM-DD, occasionally full ISO8601.
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _to_oai_date(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
