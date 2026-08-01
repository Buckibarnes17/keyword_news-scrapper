"""
backend/discovery/wp_api.py — WordPress REST API discovery strategy.

Used by 6 sites in config/site_profiles.json: niice.org.np, iids.org.np,
kachinnews.com, mmbiztoday.com, northeastlivetv.com, newslivetv.com.

VERIFIED (live, this session) facts this module is built on:
  - GET {base}/wp-json/wp/v2/posts?per_page=100&page=N
  - Response header X-WP-Total is the exact total for the query; X-WP-TotalPages is
    the page count. Both are read from response.headers to drive pagination.
    Verified: niice=1154, iids=270, kachinnews=957, northeastlivetv=41352,
    newslivetv=43897.
  - `&search={keyword}` is a real server-side filter (verified: kachinnews
    ?search=Myanmar -> X-WP-Total=145). We always push keyword server-side rather
    than fetching everything and filtering client-side.
  - `&after={ISO8601}&orderby=date&order=desc` implements the `since` parameter.
  - WordPress caps per_page at 100. Requesting a page beyond X-WP-TotalPages returns
    HTTP 400 -- that is treated as a normal end-of-pagination signal, not an error.
  - Each post has: link, title.rendered (HTML-entity encoded -> html.unescape it),
    date_gmt, excerpt.rendered, author, _links.
  - Some installs also expose /wp-json/wp/v2/pages (iids.org.np: posts AND pages
    share the flat /<slug>/ URL space, so pages can carry real content). Controlled
    by an opt-in profile flag; default is posts-only.

CAVEAT: mmbiztoday.com's profile is confidence=low; a re-probe returned HTTP 403 from
nginx on every path. That is a normal runtime condition here (site worked when the
profile was built, may not work now) -- we log it and return whatever was gathered
before the failure (typically nothing), never raise.
"""
from __future__ import annotations

import html
import logging
import time
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from .base import DiscoveredURL, DiscoveryResult, DiscoveryStrategy, register

log = logging.getLogger("keywordscout.discovery.wp_api")

# WordPress hard-caps per_page at 100.
PER_PAGE = 100


@register
class WordPressAPIStrategy(DiscoveryStrategy):
    """Discovers content via the WordPress REST API (/wp-json/wp/v2/*)."""

    name = "wp_api"

    def discover(self, keyword: Optional[str] = None, max_urls: int = 500,
                 since: Optional[datetime] = None) -> DiscoveryResult:
        result = DiscoveryResult(strategy=self.name, domain=self.domain)

        base = (self.seed_url or f"https://{self.domain}/").rstrip("/")
        post_types = ["posts"]
        wp_cfg = self.profile.get("wp_api") or {}
        if wp_cfg.get("include_pages"):
            post_types.append("pages")

        session = requests.Session()

        # northeastlivetv.com and newslivetv.com carry a logged operator override
        # (robots.txt disallows everything but respects_robots() is set False for
        # them); every other wp_api site has respects_robots() True and we honour
        # robots.txt for the wp-json path before harvesting anything.
        if self.respects_robots() and not self._robots_allow(session, base):
            result.errors.append(
                f"{self.domain}: robots.txt disallows the wp-json API path; "
                f"skipping (no operator override on file)")
            return result

        for post_type in post_types:
            try:
                self._harvest_post_type(session, base, post_type, keyword, since,
                                         max_urls, result)
            except Exception as exc:  # discover() MUST NOT raise
                result.errors.append(
                    f"{self.domain}: unexpected error harvesting {post_type}: {exc!r}")
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                result.truncated = True
                break

        result.urls = self._cap(result.urls, max_urls, result)
        return result

    # ── internals ───────────────────────────────────────────────────────────

    def _harvest_post_type(self, session: requests.Session, base: str, post_type: str,
                            keyword: Optional[str], since: Optional[datetime],
                            max_urls: int, result: DiscoveryResult) -> None:
        endpoint = f"{base}/wp-json/wp/v2/{post_type}"
        page = 1
        total_pages: Optional[int] = None

        while True:
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                result.truncated = True
                return

            params: Dict[str, Any] = {"per_page": PER_PAGE, "page": page}
            if keyword:
                # Pushed server-side, verified behaviour (kachinnews ?search=Myanmar).
                params["search"] = keyword
            if since is not None:
                params["after"] = self._iso(since)
                params["orderby"] = "date"
                params["order"] = "desc"

            try:
                resp = self._get_with_retries(session, endpoint, params)
            except requests.RequestException as exc:
                result.errors.append(
                    f"{self.domain}: network error fetching {post_type} page {page}: {exc!r}")
                return

            if resp is None:
                # All retries exhausted; error already recorded by _get_with_retries.
                return

            if resp.status_code == 403:
                # Verified real-world condition: mmbiztoday.com worked at profile time
                # but now returns 403 from nginx on every path. Log and stop cleanly.
                log.warning("%s: %s returned HTTP 403 (site may be blocking us now); "
                            "returning what was gathered so far", self.domain, endpoint)
                result.errors.append(
                    f"{self.domain}: HTTP 403 from {endpoint} (site blocked us at runtime)")
                return

            if resp.status_code == 400:
                # Normal end-of-pagination condition: page > X-WP-TotalPages.
                log.debug("%s: %s page %d returned 400 (end of pagination)",
                          self.domain, post_type, page)
                return

            if resp.status_code == 404:
                # e.g. /wp-json/wp/v2/pages not exposed on this install.
                log.info("%s: %s endpoint not found (404): %s",
                         self.domain, post_type, endpoint)
                result.errors.append(f"{self.domain}: {endpoint} returned 404")
                return

            if not resp.ok:
                result.errors.append(
                    f"{self.domain}: {endpoint} page {page} returned HTTP {resp.status_code}")
                return

            if total_pages is None:
                total_pages = self._read_total_pages(resp)

            try:
                items = resp.json()
            except ValueError as exc:
                result.errors.append(
                    f"{self.domain}: invalid JSON from {endpoint} page {page}: {exc!r}")
                return

            if not isinstance(items, list):
                result.errors.append(
                    f"{self.domain}: unexpected JSON shape from {endpoint} page {page} "
                    f"(expected list, got {type(items).__name__})")
                return

            if not items:
                # Empty page -- nothing left, stop rather than looping forever.
                return

            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                du = self._to_discovered_url(item, post_type)
                if du is not None:
                    result.urls.append(du)
                if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                    more_in_page = idx < len(items) - 1
                    more_pages = total_pages is not None and page < total_pages
                    if more_in_page or more_pages or total_pages is None:
                        # total_pages is None means we couldn't confirm this was
                        # the last page, so err on the side of flagging truncation.
                        result.truncated = True
                    return

            if total_pages is not None and page >= total_pages:
                return

            page += 1
            time.sleep(self.crawl_delay())

    def _robots_allow(self, session: requests.Session, base: str) -> bool:
        """Best-effort robots.txt check for the wp-json API path.

        Failure to fetch/parse robots.txt is treated as "allowed" (fail-open) --
        we should never turn a network hiccup into a full site block when the
        operator never recorded an override.
        """
        robots_url = urljoin(base + "/", "robots.txt")
        try:
            resp = session.get(robots_url, headers=self.headers(),
                                timeout=self.timeout())
            if not resp.ok:
                return True
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(resp.text.splitlines())
            ua = self.headers().get("User-Agent", "*")
            return rp.can_fetch(ua, base + "/wp-json/wp/v2/posts")
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
                resp = session.get(endpoint, params=params, headers=self.headers(),
                                    timeout=self.timeout())
                return resp
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
    def _read_total_pages(resp: requests.Response) -> Optional[int]:
        raw = resp.headers.get("X-WP-TotalPages")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _to_discovered_url(self, item: Dict[str, Any],
                            post_type: str) -> Optional[DiscoveredURL]:
        link = item.get("link")
        if not link or not isinstance(link, str):
            return None
        if not link.startswith(("http://", "https://")):
            return None
        if not self.host_allowed(link):
            return None

        title_raw = ((item.get("title") or {}).get("rendered")
                     if isinstance(item.get("title"), dict) else None)
        title = html.unescape(title_raw) if isinstance(title_raw, str) else None

        published_at = self._parse_date_gmt(item.get("date_gmt"))

        excerpt_raw = ((item.get("excerpt") or {}).get("rendered")
                       if isinstance(item.get("excerpt"), dict) else None)
        excerpt = html.unescape(excerpt_raw) if isinstance(excerpt_raw, str) else None

        is_article = self.classify_url(link)
        if is_article is None:
            # A WP post is inherently article-level content; a page is not assumed
            # to be (may be staff/about/contact -- classify_url decides if patterns
            # exist, otherwise fall back on post_type).
            is_article = True if post_type == "posts" else None

        metadata: Dict[str, Any] = {
            "post_type": post_type,
            "wp_id": item.get("id"),
            "author": item.get("author"),
            "excerpt": excerpt,
        }

        try:
            return DiscoveredURL(
                url=link,
                source=self.name,
                title=title,
                published_at=published_at,
                is_article=is_article,
                metadata=metadata,
            )
        except ValueError as exc:
            log.warning("%s: skipping malformed URL %r: %r", self.domain, link, exc)
            return None

    @staticmethod
    def _parse_date_gmt(raw: Any) -> Optional[datetime]:
        if not raw or not isinstance(raw, str):
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt

    @staticmethod
    def _iso(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
