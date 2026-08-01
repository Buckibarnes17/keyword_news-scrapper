"""
backend/discovery/site_search.py — site-native search discovery strategy.

Used by 6 sites in config/site_profiles.json, all declaring strategy="search":
chinadaily.com.cn, globaltimes.cn, irrawaddy.com, elevenmyanmar.com,
bnionline.net, ifa.gov.np.

Design: the per-site `search.url_template` in the profile is the single source of
truth for the request shape (never hardcode a URL here). Each site gets a small
per-domain result-link extractor because the response shape differs (chinadaily is
JSON; the rest are HTML with different DOM/pager conventions), but pagination,
politeness (crawl_delay/headers/timeout/retries) and the repeated-page stop
condition are shared.

VERIFIED per-site facts this module is built on (see config/site_profiles.json for
the full rationale + probe notes):

  - chinadaily.com.cn: newssearch.chinadaily.com.cn/rest/en/search is a JSON API
    (NOT the HTML wrapper, which needs JS) and is the ONLY live discovery route --
    its sitemap is frozen at 2014 and its RSS at 2017, and BOTH still return HTTP
    200, so a naive "sitemap 200 -> use it" pipeline is a trap. sort=dp is
    newest-first; `page` is 0-indexed; each record carries url/title/source/
    publishTime (epoch ms)/plainText.

  - globaltimes.cn: search.globaltimes.cn/SearchCtrl is GET-addressable despite the
    on-page <form method=post>. Default ordering is OLDEST-first (the first page is
    2009 content) and the orderByTime param does not change that -- so we always
    pass begin_date/end_date to scope to recent content instead of trusting sort.

  - irrawaddy.com: www is Cloudflare-blocked (403 on every path incl. XML/JSON
    endpoints). Search works ONLY on burma.irrawaddy.com with `?s={kw}&paged={n}`
    -- note `&paged=N`, NOT `/page/N/` (that form is 403). Crawl-delay is 10s
    (self.crawl_delay() reads it from the profile). CRITICAL, verified directly:
    burma.irrawaddy.com returns 403 with a bare User-Agent but 200 with the full
    browser header set -- self.headers() always returns that full set, so we must
    never build a request with anything less (e.g. no custom "User-Agent"-only dict).

  - elevenmyanmar.com / bnionline.net: Drupal 7, `/search/node/{kw}?page=N` (and the
    bnionline `/en/` prefix), both paginate. elevenmyanmar's pager reflects arbitrary
    query strings back into href attributes (a log4j-style injection payload was
    found there during profiling) -- we NEVER follow scraped pager hrefs; every
    pager URL is constructed from the profile template plus an integer page index.

  - ifa.gov.np: the homepage search form is a decoy -- `/en/?q=Nepal` and
    `/en/?q=zzzzqqq` return byte-identical homepages. Real search only works on the
    listing endpoints, so this strategy queries BOTH `/en/news/?q=` and
    `/en/publications/?q=` (the profile's url_template covers publications; the news
    endpoint is derived by swapping the path segment, since both are documented as
    verified in the profile notes). Only ~50 real documents exist site-wide.

Pagination stop conditions (checked every page, any one stops the walk):
  - empty result set on the page
  - the page's extracted links are identical to the previous page's (these sites
    loop rather than 404 on out-of-range pages)
  - max_urls reached
  - a hard per-site page cap (defensive upper bound, independent of the above)
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus

import requests
from lxml import etree, html as lxml_html

from .base import DiscoveredURL, DiscoveryResult, DiscoveryStrategy, register

log = logging.getLogger("keywordscout.discovery.site_search")

# Defensive upper bound on pages walked per query, independent of max_urls --
# guards against a site whose pager never truly repeats but also never ends.
HARD_PAGE_CAP = 60


@register
class SiteSearchStrategy(DiscoveryStrategy):
    """Discovers content via each site's own search endpoint.

    The profile's search.url_template is substituted with {keyword} (URL-encoded
    via quote_plus) and {page}; the per-site _extract_* method turns the response
    into (url, title, published_at) tuples.
    """

    name = "search"

    # Per-site page-index base: most of these sites start pagination at 0 or 1.
    _PAGE_START = {
        "chinadaily.com.cn": 0,   # `page` is documented 0-indexed
        "globaltimes.cn": 1,
        "irrawaddy.com": 0,       # &paged=0 and &paged=1 both return page 1 in WP;
                                    # we still start at 0 defensively and dedupe.
        "elevenmyanmar.com": 0,
        "bnionline.net": 0,
        "ifa.gov.np": 1,
    }

    def discover(self, keyword: Optional[str] = None, max_urls: int = 500,
                 since: Optional[datetime] = None) -> DiscoveryResult:
        result = DiscoveryResult(strategy=self.name, domain=self.domain)
        try:
            self._discover_inner(keyword, max_urls, since, result)
        except Exception as exc:  # discover() MUST NOT raise
            result.errors.append(f"{self.domain}: unexpected top-level error: {exc!r}")
        result.urls = self._cap(result.urls, max_urls, result)
        return result

    # ── dispatch ────────────────────────────────────────────────────────────

    def _discover_inner(self, keyword: Optional[str], max_urls: int,
                         since: Optional[datetime], result: DiscoveryResult) -> None:
        if not keyword:
            result.errors.append(
                f"{self.domain}: search strategy requires a keyword; none given")
            return

        search_cfg = self.profile.get("search") or {}
        if not search_cfg.get("supported"):
            result.errors.append(f"{self.domain}: profile marks search unsupported")
            return

        template = search_cfg.get("url_template")
        if not template:
            result.errors.append(f"{self.domain}: no search.url_template in profile")
            return

        session = requests.Session()

        if self.domain == "ifa.gov.np":
            # Two listing endpoints are the only real search surfaces (the
            # homepage /en/?q= form is a verified decoy -- see module docstring).
            self._walk_ifa(session, template, keyword, max_urls, since, result)
            return

        extractor = {
            "chinadaily.com.cn": self._extract_chinadaily,
            "globaltimes.cn": self._extract_globaltimes,
            "irrawaddy.com": self._extract_drupal_or_wp_html,
            "elevenmyanmar.com": self._extract_drupal_or_wp_html,
            "bnionline.net": self._extract_drupal_or_wp_html,
        }.get(self.domain)

        if extractor is None:
            result.errors.append(
                f"{self.domain}: no result extractor registered for this domain")
            return

        if self.domain == "globaltimes.cn":
            # The template embeds the SAME literal placeholder "{YYYY-MM-DD}" twice
            # (once for begin_date, once for end_date) so a plain str.format/replace
            # cannot disambiguate them positionally-safe generic substitution is used
            # instead. Verified: default ordering is OLDEST-first and orderByTime did
            # NOT change that in profiling, so we MUST scope with begin_date/end_date
            # rather than relying on sort -- otherwise we harvest 2009 content.
            begin, end = self._globaltimes_date_range(since)

            def url_builder(page: int) -> Optional[str]:
                return self._build_globaltimes_url(template, keyword, page, begin, end)
        else:
            def url_builder(page: int) -> Optional[str]:
                return self._build_url(template, keyword, page)

        self._walk_paginated(session, url_builder, extractor, max_urls, since, result)

    # ── generic pagination walk ─────────────────────────────────────────────

    def _walk_paginated(self, session: requests.Session, url_builder, extractor,
                         max_urls: int, since: Optional[datetime],
                         result: DiscoveryResult) -> None:
        page = self._PAGE_START.get(self.domain, 0)
        prev_links: Optional[Tuple[str, ...]] = None
        pages_walked = 0

        while pages_walked < HARD_PAGE_CAP:
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                return

            url = url_builder(page)
            if url is None:
                result.errors.append(
                    f"{self.domain}: could not build search URL for page {page}")
                return

            resp = self._get_with_retries(session, url, result)
            if resp is None:
                return  # error already recorded

            if resp.status_code == 404:
                log.info("%s: search page %d returned 404 (end of pagination)",
                          self.domain, page)
                return
            if not resp.ok:
                result.errors.append(
                    f"{self.domain}: search page {page} returned HTTP {resp.status_code}")
                return

            try:
                items = extractor(resp, url)
            except Exception as exc:
                result.errors.append(
                    f"{self.domain}: failed to parse search page {page}: {exc!r}")
                return

            if not items:
                log.debug("%s: search page %d empty; stopping", self.domain, page)
                return

            link_tuple = tuple(sorted(u for (u, _t, _d) in items))
            if prev_links is not None and link_tuple == prev_links:
                log.info("%s: search page %d repeats the previous page's links; "
                         "stopping to avoid an infinite loop", self.domain, page)
                return
            prev_links = link_tuple

            new_count = 0
            for (u, title, published_at) in items:
                if since is not None and published_at is not None and published_at < since:
                    continue
                du = self._make_discovered_url(u, title, published_at)
                if du is not None:
                    result.urls.append(du)
                    new_count += 1
                if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                    return

            pages_walked += 1
            page += 1
            time.sleep(self.crawl_delay())

    def _walk_ifa(self, session: requests.Session, publications_template: str,
                  keyword: str, max_urls: int, since: Optional[datetime],
                  result: DiscoveryResult) -> None:
        # Verified endpoints: /en/publications/?q={kw} (the profile's template) and
        # /en/news/?q={kw} (documented working in the same profile notes). The
        # homepage /en/?q= is a decoy and MUST NOT be used.
        news_template = publications_template.replace("/publications/", "/news/")
        templates = [publications_template]
        if news_template != publications_template:
            templates.append(news_template)

        for template in templates:
            prev_links: Optional[Tuple[str, ...]] = None
            page = 1
            pages_walked = 0
            while pages_walked < HARD_PAGE_CAP:
                if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                    return
                url = self._build_ifa_url(template, keyword, page)
                if url is None:
                    result.errors.append(
                        f"{self.domain}: could not build IFA search URL for {template!r}")
                    break
                resp = self._get_with_retries(session, url, result)
                if resp is None:
                    break
                if not resp.ok:
                    result.errors.append(
                        f"{self.domain}: {url} returned HTTP {resp.status_code}")
                    break
                try:
                    items = self._extract_ifa(resp, url)
                except Exception as exc:
                    result.errors.append(f"{self.domain}: failed to parse {url}: {exc!r}")
                    break
                if not items:
                    break
                link_tuple = tuple(sorted(u for (u, _t, _d) in items))
                if prev_links is not None and link_tuple == prev_links:
                    log.info("%s: %s page %d repeats previous page; stopping",
                             self.domain, template, page)
                    break
                prev_links = link_tuple
                for (u, title, published_at) in items:
                    if since is not None and published_at is not None and published_at < since:
                        continue
                    du = self._make_discovered_url(u, title, published_at)
                    if du is not None:
                        result.urls.append(du)
                    if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                        return
                pages_walked += 1
                page += 1
                time.sleep(self.crawl_delay())

    # ── URL building ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_url(template: str, keyword: str, page: int) -> Optional[str]:
        try:
            encoded_kw = quote_plus(keyword)
            return template.format(keyword=encoded_kw, page=page)
        except (KeyError, IndexError, ValueError):
            return None

    @staticmethod
    def _build_ifa_url(template: str, keyword: str, page: int) -> Optional[str]:
        """The verified ifa.gov.np search.url_template carries only {keyword} (no
        {page} placeholder) -- pagination beyond page 1 is not documented as
        verified, so we append `&page=N` in the same convention every other
        listing endpoint on this site uses, only for page > 1."""
        try:
            base = template.format(keyword=quote_plus(keyword))
        except (KeyError, IndexError, ValueError):
            return None
        if page <= 1:
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page}"

    @staticmethod
    def _globaltimes_date_range(since: Optional[datetime]) -> Tuple[str, str]:
        """Returns (begin_date, end_date) as YYYY-MM-DD strings. Defaults to the
        last 90 days when `since` is not given -- verified default ordering is
        oldest-first, so an unscoped query would otherwise return 2009 content."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        end = now.date()
        if since is not None:
            begin = since.astimezone(timezone.utc).date() if since.tzinfo else since.date()
        else:
            begin = (now - timedelta(days=90)).date()
        return begin.isoformat(), end.isoformat()

    @staticmethod
    def _build_globaltimes_url(template: str, keyword: str, page: int,
                                begin_date: str, end_date: str) -> Optional[str]:
        """Substitutes {keyword} and {page} normally, then fills the two literal
        "{YYYY-MM-DD}" occurrences positionally: first -> begin_date, second ->
        end_date (the template's own param order is begin_date then end_date)."""
        try:
            partial = template.format(keyword=quote_plus(keyword), page=page,
                                       **{"YYYY-MM-DD": "{YYYY-MM-DD}"})
        except (KeyError, IndexError, ValueError):
            return None
        placeholder = "{YYYY-MM-DD}"
        first = partial.find(placeholder)
        if first == -1:
            # Template didn't carry the date placeholder at all; still usable.
            return partial
        partial = partial[:first] + begin_date + partial[first + len(placeholder):]
        second = partial.find(placeholder)
        if second != -1:
            partial = partial[:second] + end_date + partial[second + len(placeholder):]
        return partial

    def _get_with_retries(self, session: requests.Session, url: str,
                           result: DiscoveryResult) -> Optional[requests.Response]:
        attempts = max(1, self.max_retries() + 1)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                resp = session.get(url, headers=self.headers(), timeout=self.timeout())
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s: attempt %d/%d failed for %s: %r",
                            self.domain, attempt + 1, attempts, url, exc)
                if attempt < attempts - 1:
                    time.sleep(self.crawl_delay())
        result.errors.append(f"{self.domain}: all retries failed for {url}: {last_exc!r}")
        return None

    def _make_discovered_url(self, url: str, title: Optional[str],
                              published_at: Optional[datetime]) -> Optional[DiscoveredURL]:
        if not url or not url.startswith(("http://", "https://")):
            return None
        if not self.host_allowed(url):
            return None
        is_article = self.classify_url(url)
        try:
            return DiscoveredURL(
                url=url,
                source=self.name,
                title=title,
                published_at=published_at,
                is_article=is_article,
                metadata={},
            )
        except ValueError as exc:
            log.warning("%s: skipping malformed URL %r: %r", self.domain, url, exc)
            return None

    # ── per-site extractors: each returns List[(url, title, published_at)] ──

    def _extract_chinadaily(self, resp: requests.Response,
                             request_url: str) -> List[Tuple[str, Optional[str],
                                                              Optional[datetime]]]:
        # JSON API: {"content": [{"url":..., "title":..., "publishTime": <ms>, ...}]}
        # or similar top-level shape -- be defensive, the exact envelope is not
        # documented beyond the profile's example fields (url/title/source/
        # publishTime/plainText), so we scan for a list of dicts wherever it lives.
        try:
            data = resp.json()
        except ValueError:
            return []
        records = self._find_record_list(data)
        out = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            url = rec.get("url")
            if not isinstance(url, str) or not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = "https:" + url if url.startswith("//") else None
            if not url:
                continue
            title = rec.get("title") if isinstance(rec.get("title"), str) else None
            published_at = self._epoch_ms_to_dt(rec.get("publishTime"))
            out.append((url, title, published_at))
        return out

    @staticmethod
    def _find_record_list(data: Any) -> List[Any]:
        """Locate the list-of-article-dicts inside an unknown JSON envelope shape."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("content", "list", "data", "results", "items"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
                if isinstance(val, dict):
                    nested = SiteSearchStrategy._find_record_list(val)
                    if nested:
                        return nested
        return []

    @staticmethod
    def _epoch_ms_to_dt(raw: Any) -> Optional[datetime]:
        try:
            ms = float(raw)
        except (TypeError, ValueError):
            return None
        try:
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def _extract_globaltimes(self, resp: requests.Response,
                              request_url: str) -> List[Tuple[str, Optional[str],
                                                               Optional[datetime]]]:
        try:
            tree = lxml_html.fromstring(resp.content)
        except (etree.ParserError, ValueError):
            return []
        out = []
        seen = set()
        for a in tree.xpath('//a[@href]'):
            href = a.get("href") or ""
            if not re.search(r"/page/\d{6}/\d+\.shtml$", href):
                continue
            full = self._absolutize(href, "https://www.globaltimes.cn")
            if full in seen:
                continue
            seen.add(full)
            title = (a.text_content() or "").strip() or None
            published_at = self._date_from_globaltimes_path(full)
            out.append((full, title, published_at))
        return out

    @staticmethod
    def _date_from_globaltimes_path(url: str) -> Optional[datetime]:
        m = re.search(r"/page/(\d{4})(\d{2})/\d+\.shtml", url)
        if not m:
            return None
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc)
        except ValueError:
            return None

    def _extract_drupal_or_wp_html(self, resp: requests.Response,
                                    request_url: str) -> List[Tuple[str, Optional[str],
                                                                     Optional[datetime]]]:
        """Shared HTML link extractor for elevenmyanmar/bnionline (Drupal search
        results) and irrawaddy (WP ?s= results). Pulls candidate article links using
        each domain's article_url_patterns from the profile so we do not depend on
        any particular result-list CSS structure (which differs per site and is not
        verified in the profile beyond "returns N distinct article links")."""
        try:
            tree = lxml_html.fromstring(resp.content)
        except (etree.ParserError, ValueError):
            return []
        base = f"https://{self.domain}"
        patterns = self.profile.get("article_url_patterns") or []
        out = []
        seen = set()
        for a in tree.xpath('//a[@href]'):
            href = a.get("href") or ""
            full = self._absolutize(href, base)
            if not full or full in seen:
                continue
            if not any(self._safe_search(p, full) for p in patterns):
                continue
            seen.add(full)
            title = (a.text_content() or "").strip() or None
            out.append((full, title, None))  # no reliable date in search-result HTML
        return out

    @staticmethod
    def _safe_search(pattern: str, s: str) -> bool:
        try:
            return bool(re.search(pattern, s))
        except re.error:
            return False

    def _extract_ifa(self, resp: requests.Response,
                      request_url: str) -> List[Tuple[str, Optional[str],
                                                       Optional[datetime]]]:
        try:
            tree = lxml_html.fromstring(resp.content)
        except (etree.ParserError, ValueError):
            return []
        patterns = self.profile.get("article_url_patterns") or []
        out = []
        seen = set()
        for a in tree.xpath('//a[@href]'):
            href = a.get("href") or ""
            full = self._absolutize(href, "https://ifa.gov.np")
            if not full or full in seen:
                continue
            if not any(self._safe_search(p, full) for p in patterns):
                continue
            # Decoy guard: ifa.gov.np's homepage decoy always resolves to the exact
            # listing root; genuine hits are detail pages under /en/news/<slug>/ or
            # /en/publications/<slug>/, which the article_url_patterns already
            # enforce, but we defensively also reject bare listing roots here.
            if full.rstrip("/").endswith(("/en/news", "/en/publications")):
                continue
            seen.add(full)
            title = (a.text_content() or "").strip() or None
            out.append((full, title, None))
        return out

    @staticmethod
    def _absolutize(href: str, base: str) -> Optional[str]:
        if not href:
            return None
        href = href.strip()
        if href.startswith("//"):
            return "https:" + href
        if href.startswith(("http://", "https://")):
            return href
        if href.startswith("/"):
            return base.rstrip("/") + href
        return None
