"""
backend/discovery/news_sitemap.py — NewsSitemapStrategy: Google News (<news:news>)
sitemap discovery, scoped to a site's news section when the sitemap surface is
site-wide.

Used by cgtn.com, burmese.voanews.com, bbc.com (Burmese section only), rfa.org
(Burmese), kathmandupost.com — see config/site_profiles.json "strategy": "news_sitemap".

Builds directly on backend/discovery/sitemap.py's shared XML parsing, gzip handling,
robots.txt cache and crawl4ai bridge; this module adds:
  - scope-prefix filtering (bbc.com's/rfa.org's news sitemaps are site-wide across
    all languages — this is what stops discovery exploding into all of bbc.com)
  - kathmandupost.com's "probe past the advertised sitemap-index end" behaviour
  - the crawl4ai AsyncUrlSeeder primary path with the #1306 relevance_score bug
    handled in sitemap.entries_from_seeder_results()

crawl4ai is NOT installed on this machine; CRAWL4AI_AVAILABLE mirrors the guard in
sitemap.py so this module can be imported and exercised (via its XML fallback path)
without crawl4ai present.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from .base import DiscoveredURL, DiscoveryResult, DiscoveryStrategy, register
from .sitemap import (
    CRAWL4AI_AVAILABLE,
    RobotsAwareMixin,
    SitemapEntry,
    _TRAILING_NUM_RE,
    _keyword_score_or_none,
    entries_from_seeder_results,
    parse_datetime,
    probe_numeric_continuation,
    seed_via_crawl4ai,
    walk_sitemap_tree,
)

log = logging.getLogger("keywordscout.discovery.news_sitemap")


@register
class NewsSitemapStrategy(RobotsAwareMixin, DiscoveryStrategy):
    """Google News sitemap (<news:news>, <news:publication_date>) discovery.

    Two sites in this batch (bbc.com, rfa.org) publish a sitemap that spans the whole
    site/all languages; scope it down with `_scope_prefix()` (explicit
    profile["scope_prefix"], else derived from the seed_url's path — bbc.com's
    seed_url https://www.bbc.com/burmese and rfa.org's https://www.rfa.org/burmese/
    both derive "/burmese" this way). Sites that don't need scoping (cgtn.com is
    fenced by allowed_hosts instead; burmese.voanews.com and kathmandupost.com are
    already dedicated hosts) get scope_prefix=None and pass everything through.
    """
    name = "news_sitemap"

    def discover(self, keyword: Optional[str] = None, max_urls: int = 500,
                 since: Optional[datetime] = None) -> DiscoveryResult:
        result = DiscoveryResult(strategy=self.name, domain=self.domain)
        try:
            scope_prefix = self._scope_prefix()
            urls: List[DiscoveredURL] = []
            if CRAWL4AI_AVAILABLE:
                try:
                    pattern = f"*{scope_prefix}*" if scope_prefix else "*"
                    delay = self.crawl_delay()
                    hits_per_sec = (1.0 / delay) if delay > 0 else 5.0
                    raw = seed_via_crawl4ai(self.domain, pattern, keyword, max_urls, hits_per_sec)
                    if raw:
                        urls = entries_from_seeder_results(
                            raw, source_name=self.name, keyword=keyword,
                            host_allowed=self.host_allowed, classify_url=self.classify_url,
                            scope_prefix=scope_prefix, since=since)
                except Exception as exc:
                    log.warning("%s: crawl4ai seeding failed (%s); falling back to XML walk",
                                self.domain, exc)
                    result.errors.append(f"crawl4ai path failed: {exc!r}")
            if not urls:
                urls = self._discover_via_xml(keyword, max_urls, since, scope_prefix, result)
            result.urls = self._cap(urls, max_urls, result)
        except Exception as exc:
            log.exception("%s: news_sitemap discover() failed", self.domain)
            result.errors.append(f"discover() failed: {exc!r}")
        return result

    # ── scoping ─────────────────────────────────────────────────────────────────
    def _scope_prefix(self) -> Optional[str]:
        explicit = self.profile.get("scope_prefix")
        if explicit:
            return explicit
        try:
            path = urlparse(self.seed_url).path.rstrip("/")
        except Exception:
            path = ""
        if path:
            log.warning(
                "%s: no scope_prefix declared in profile; derived %r from seed_url "
                "%r. This is what keeps a site-wide news sitemap (e.g. bbc.com, "
                "rfa.org) from exploding into every language/section on the site — "
                "verify it matches the intended section.",
                self.domain, path, self.seed_url)
            return path
        return None

    def _seed_urls(self) -> List[str]:
        seeds = [e.get("url") for e in (self.profile.get("sitemaps") or []) if e.get("url")]
        seeds = list(dict.fromkeys(seeds))
        if not seeds:
            derived = f"https://{self.domain}/sitemap.xml"
            log.warning("%s: no sitemaps declared in profile; guessing %s", self.domain, derived)
            seeds = [derived]
        return seeds

    # ── pure-Python fallback ────────────────────────────────────────────────────
    def _discover_via_xml(self, keyword: Optional[str], max_urls: int,
                           since: Optional[datetime], scope_prefix: Optional[str],
                           result: DiscoveryResult) -> List[DiscoveredURL]:
        session = requests.Session()
        headers, timeout = self.headers(), self.timeout()
        retries, delay = self.max_retries(), self.crawl_delay()
        seen_urls: set = set()
        discovered: List[DiscoveredURL] = []
        overcollect_cap = max(max_urls * 5, 5000) if max_urls and max_urls > 0 else 50000

        def in_scope(url: str) -> bool:
            if not scope_prefix:
                return True
            try:
                return urlparse(url).path.startswith(scope_prefix)
            except Exception:
                return False

        def on_entries(entries: List[SitemapEntry]) -> bool:
            for e in entries:
                if len(discovered) >= overcollect_cap:
                    return True
                url = e.loc
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                # Host preservation for cgtn.com: never rewrite e.loc, and rely on
                # host_allowed()'s profile-declared allowed_hosts=["*.cgtn.com"] to
                # accept news*.cgtn.com while a same-registrable-domain default
                # would (per the profile notes) still need www.cgtn.com never to be
                # treated as the article host. We just trust whatever host the
                # sitemap gave us.
                if not self.host_allowed(url) or not in_scope(url):
                    continue
                is_article = self.classify_url(url)
                if is_article is False:
                    continue
                # Publication date preference order per the base.py contract:
                # <news:publication_date> > <lastmod>. (JSON-LD datePublished is a
                # third option only available via the crawl4ai head-extraction path.)
                published_at = parse_datetime(e.news_pubdate) or parse_datetime(e.lastmod)
                if since and published_at and published_at < since:
                    continue
                score = _keyword_score_or_none(e.news_title or "", url, keyword)
                discovered.append(DiscoveredURL(
                    url=url, source=self.name, title=e.news_title, published_at=published_at,
                    relevance_score=score, is_article=is_article,
                    metadata={"lastmod": e.lastmod, "via": "xml"}))
            return bool(max_urls and max_urls > 0 and len(discovered) >= max_urls and since is None)

        visited = walk_sitemap_tree(
            session, self._seed_urls(), headers=headers, timeout=timeout, retries=retries,
            delay=delay, robots_allowed=self.robots_allowed, on_entries=on_entries,
            errors=result.errors)

        already_full = bool(max_urls and max_urls > 0 and len(discovered) >= max_urls)
        if not already_full:
            self._probe_past_index_end(session, visited, headers, timeout, retries, delay,
                                        on_entries)
        return discovered

    def _probe_past_index_end(self, session: requests.Session, visited: "set[str]",
                               headers: Dict[str, str], timeout: int, retries: int,
                               delay: float, on_entries) -> None:
        """kathmandupost.com's /sitemap/ index advertises only 100 children
        (news/1..news/99 + category) but pages 100-161 exist and were found by
        binary search. For every numeric-tailed sitemap URL we actually fetched,
        find the highest number seen per (prefix, suffix) group and keep probing
        upward until a fetch fails or comes back empty. Harmless on sites where the
        index IS complete: the very next probe 404s/empties out immediately.
        """
        import time as _time  # local import: only this helper needs the delay sleep

        by_group: Dict[Tuple[str, str], int] = {}
        for sm_url in visited:
            m = _TRAILING_NUM_RE.match(sm_url)
            if not m:
                continue
            prefix, num_str, suffix = m.groups()
            key = (prefix, suffix)
            n = int(num_str)
            if n > by_group.get(key, -1):
                by_group[key] = n

        for (prefix, suffix), n in by_group.items():
            last_url = f"{prefix}{n}{suffix}"
            for _candidate, entries in probe_numeric_continuation(
                    session, last_url, headers, timeout, retries):
                _time.sleep(delay)
                if on_entries(entries):
                    break
