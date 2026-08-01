"""
backend/discovery/deep_crawl.py — deep-crawl discovery strategy.

Used by 4 ACTIVE sites in config/site_profiles.json declaring strategy="deep_crawl":
fmprc.gov.cn, stats.gov.cn, mizzima.com, hornbilltv.com.
(ndri.org.np also references deep_crawl conceptually but is strategy="disabled" in
the profile -- get_strategy() already returns None for disabled profiles, so this
module does not special-case it.)

crawl4ai integration
---------------------
crawl4ai is NOT installed on this machine, so the BestFirstCrawlingStrategy /
KeywordRelevanceScorer / FilterChain path below is written strictly from
CRAWL4AI_API.md and is guarded end-to-end: every import is wrapped in try/except,
CRAWL4AI_AVAILABLE gates all use of it, and a pure requests+lxml BFS fallback
(_bfs_fallback) is the code path that actually runs and is tested here. The exact
filter/scorer import paths are marked UNVERIFIED in CRAWL4AI_API.md ("docs show the
class names and usage but the module layout may differ") so those imports are
individually try/excepted and any that fail degrade to None without aborting.
Async crawl4ai calls are bridged into this module's sync discover() contract via
asyncio.run(), with a check for an already-running event loop (discover() is called
from inside backend's ThreadPoolExecutor-based pipeline, which does not itself run
an event loop, but asyncio.run() raises RuntimeError if one somehow is -- we detect
that rather than letting it explode).

VERIFIED per-site facts this module is built on (see config/site_profiles.json for
full rationale + probe notes):

  - fmprc.gov.cn: seed is /eng/ (the profile already corrects the historical /en/
    bug). CRITICAL TRAP verified directly: every unknown path on fmprc.gov.cn /
    mfa.gov.cn 302s to https://www.mfa.gov.cn/web/system/index_17321.shtml, a
    Chinese-language maintenance page that itself returns HTTP 200 -- so
    /sitemap.xml, /rss, /robots.txt all "succeed" while carrying nothing but that
    one page. We detect this by checking response.url (post-redirect) for
    "mfa.gov.cn/web/system/" and by a documented fixed byte length (55990 bytes,
    used as a secondary signal since exact byte counts can drift) and treat either
    as a miss, never as content. Listing pages paginate via index_N.html.

  - stats.gov.cn: hostile transport. IPv6 blackhole (DNS returns AAAA but there is
    no v6 route on this host) is handled by forcing AF_INET via
    urllib3.util.connection.allowed_gai_family. Port 80 is firewalled while the
    site's own internal links are written http:// -- every discovered link is
    rewritten to https:// before being queued or returned. The QiAnXin WAF gives
    ~7.5s handshakes and ~35% success, so the profile's timeout_s=60 / max_retries=5
    are read via self.timeout()/self.max_retries() rather than hardcoded.

  - mizzima.com: Cloudflare managed challenge (cf-mitigated: challenge header) on
    every host/path tried during profiling; only one stale RSS item was
    discoverable at all. We detect the challenge (cf-mitigated header, or a 403/503
    with the Cloudflare challenge markers in the body) and return an EMPTY result
    with a clear message in result.errors rather than retrying into it repeatedly.

  - hornbilltv.com: articles are addressable by numeric ID alone -- /x/y/233457
    serves the same content as the full slug URL, and IDs are sequential with a
    verified head near 233,465. We implement incremental discovery by walking IDs
    downward from a head (either a caller-supplied hint via profile/metadata or the
    verified approximate head) rather than crawling listing pages. Its
    /topics/{keyword} search returns real hits (26 for "Assam") but its pagination
    is fake (?page=2/?page=20 return the identical first page) -- so when a keyword
    is given we fetch exactly ONE page of /topics/{keyword} and do not paginate it.
"""
from __future__ import annotations

import logging
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree, html as lxml_html

from .base import DiscoveredURL, DiscoveryResult, DiscoveryStrategy, register

log = logging.getLogger("keywordscout.discovery.deep_crawl")

# ── crawl4ai: fully optional, guarded exactly like SELENIUM_AVAILABLE in
#    backend/crawler.py. Nothing below may assume these names are usable. ──────
CRAWL4AI_AVAILABLE = False
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig  # noqa: F401
    from crawl4ai.deep_crawling import BestFirstCrawlingStrategy  # noqa: F401
    CRAWL4AI_AVAILABLE = True
except ImportError:
    AsyncWebCrawler = None  # type: ignore
    BrowserConfig = None  # type: ignore
    CrawlerRunConfig = None  # type: ignore
    BestFirstCrawlingStrategy = None  # type: ignore

# UNVERIFIED import paths per CRAWL4AI_API.md section 2 ("exact filter/scorer
# import paths are UNVERIFIED ... wrap imports in try/except and degrade
# gracefully"). Each is independently optional.
KeywordRelevanceScorer = None  # type: ignore
if CRAWL4AI_AVAILABLE:
    try:
        from crawl4ai.deep_crawling.scorers import KeywordRelevanceScorer  # noqa: F401,E501
    except ImportError:
        log.info("crawl4ai present but KeywordRelevanceScorer import path unverified/"
                  "unavailable; scoring will fall back to our own keyword heuristic")

FilterChain = None  # type: ignore
DomainFilter = None  # type: ignore
URLPatternFilter = None  # type: ignore
if CRAWL4AI_AVAILABLE:
    try:
        from crawl4ai.deep_crawling.filters import (  # noqa: F401
            FilterChain, DomainFilter, URLPatternFilter)
    except ImportError:
        log.info("crawl4ai present but filter import path unverified/unavailable; "
                  "filtering will be done manually via host_allowed()/classify_url()")


# ── IPv4 forcing for stats.gov.cn (its AAAA record is a blackhole on many
#    egress networks). Applied only when this module actually crawls that
#    domain, and only once per process. ─────────────────────────────────────
_IPV4_FORCED = False


def _force_ipv4_once() -> None:
    global _IPV4_FORCED
    if _IPV4_FORCED:
        return
    try:
        import urllib3.util.connection as urllib3_cn
        urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
        _IPV4_FORCED = True
        log.info("forced IPv4-only DNS resolution (urllib3) for hostile-transport sites")
    except Exception as exc:  # pragma: no cover -- defensive; never fatal
        log.warning("could not force IPv4 resolution: %r", exc)


# fmprc's soft-404 maintenance page: verified redirect target + a documented byte
# length used only as a secondary signal (exact length may drift over time).
_FMPRC_MAINTENANCE_MARKER = "mfa.gov.cn/web/system/"
_FMPRC_MAINTENANCE_LEN_HINT = 55990

# Cloudflare managed-challenge signals (mizzima.com).
_CF_CHALLENGE_HEADER = "cf-mitigated"
_CF_CHALLENGE_BODY_MARKERS = (
    "Just a moment", "cf-chl-", "challenges.cloudflare.com", "Checking your browser",
)

# hornbilltv: verified approximate head ID at profiling time (2026-08-01). Used only
# as a starting point when no fresher hint is available; real deployments should
# pass a more recent head via profile metadata (`deep_crawl.hornbilltv_head_id`) or
# derive it from a homepage poll, but this module never fabricates one out of thin
# air beyond this documented, dated estimate.
_HORNBILLTV_VERIFIED_HEAD_ID = 233465
_HORNBILLTV_ID_WALK_COUNT = 200  # how many sequential IDs to probe per call


@register
class DeepCrawlStrategy(DiscoveryStrategy):
    """Deep-crawl discovery: crawl4ai BestFirst when available, else a pure
    requests+lxml BFS fallback -- plus site-specific handling for the documented
    traps (fmprc soft-404, stats.gov.cn transport, mizzima Cloudflare challenge,
    hornbilltv numeric-ID walking)."""

    name = "deep_crawl"

    MAX_DEPTH_DEFAULT = 2
    MAX_PAGES_DEFAULT = 60

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
        if self.domain == "hornbilltv.com":
            self._discover_hornbilltv(keyword, max_urls, since, result)
            return

        if self.domain == "stats.gov.cn":
            _force_ipv4_once()

        session = requests.Session()

        if self.domain == "mizzima.com":
            if self._check_cloudflare_challenge(session, result):
                return  # errors already recorded; nothing further is fetchable

        # crawl4ai path (only ever exercised when the package is actually
        # installed, which it is not on this build machine -- see module docstring).
        if CRAWL4AI_AVAILABLE:
            try:
                urls = self._run_crawl4ai(keyword, max_urls, result)
                if urls:
                    for du in urls:
                        result.urls.append(du)
                        if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                            return
                    return
                log.info("%s: crawl4ai path returned nothing usable; falling back "
                         "to requests+lxml BFS", self.domain)
            except Exception as exc:
                result.errors.append(
                    f"{self.domain}: crawl4ai path failed ({exc!r}); "
                    f"falling back to requests+lxml BFS")

        self._bfs_fallback(session, keyword, max_urls, since, result)

    # ── crawl4ai (best-effort; not exercised on this machine) ───────────────

    def _run_crawl4ai(self, keyword: Optional[str], max_urls: int,
                       result: DiscoveryResult) -> List[DiscoveredURL]:
        """Attempts the crawl4ai BestFirstCrawlingStrategy path. Only reachable
        when CRAWL4AI_AVAILABLE is True. Bridges the async crawl4ai API into this
        sync method via asyncio.run(), detecting an already-running loop instead
        of letting asyncio.run() raise inside it."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no running loop -- the expected case in this sync pipeline
        else:
            # We are already inside an event loop (e.g. some future async caller).
            # asyncio.run() would raise here; do not attempt it.
            result.errors.append(
                f"{self.domain}: crawl4ai path skipped -- already inside a running "
                f"event loop, cannot nest asyncio.run()")
            return []

        try:
            return asyncio.run(self._run_crawl4ai_async(keyword, max_urls))
        except Exception as exc:
            result.errors.append(f"{self.domain}: asyncio.run(crawl4ai) failed: {exc!r}")
            return []

    async def _run_crawl4ai_async(self, keyword: Optional[str],
                                   max_urls: int) -> List[DiscoveredURL]:
        # NOTE: per CRAWL4AI_API.md, check_robots_txt defaults to False and must be
        # set True explicitly to preserve this pipeline's existing robots behaviour.
        scorer = None
        if KeywordRelevanceScorer is not None and keyword:
            try:
                scorer = KeywordRelevanceScorer(keywords=[keyword], weight=0.7)
            except Exception as exc:
                log.info("%s: KeywordRelevanceScorer construction failed (%r); "
                         "continuing without scoring", self.domain, exc)

        filter_chain = None
        if FilterChain is not None and DomainFilter is not None:
            try:
                filter_chain = FilterChain([DomainFilter(domain=self.domain)])
            except Exception as exc:
                log.info("%s: FilterChain construction failed (%r); continuing "
                         "without a filter chain", self.domain, exc)

        strategy_kwargs: Dict[str, Any] = {
            "max_depth": self.MAX_DEPTH_DEFAULT,
            "max_pages": min(self.MAX_PAGES_DEFAULT, max_urls or self.MAX_PAGES_DEFAULT),
        }
        if scorer is not None:
            strategy_kwargs["url_scorer"] = scorer
        if filter_chain is not None:
            strategy_kwargs["filter_chain"] = filter_chain

        deep_strategy = BestFirstCrawlingStrategy(**strategy_kwargs)
        run_cfg = CrawlerRunConfig(check_robots_txt=self.respects_robots())
        browser_cfg = BrowserConfig(headless=True, verbose=False)

        out: List[DiscoveredURL] = []
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            crawl_result = await crawler.arun(self.seed_url, config=run_cfg,
                                               deep_crawl_strategy=deep_strategy)
            # crawl4ai's return shape for deep crawls is not pinned down beyond
            # "list-like of results with .url" in the docs excerpt available to us;
            # be maximally defensive.
            items = crawl_result if isinstance(crawl_result, list) else [crawl_result]
            for item in items:
                url = getattr(item, "url", None) or (
                    item.get("url") if isinstance(item, dict) else None)
                if not url or not self.host_allowed(url):
                    continue
                out.append(DiscoveredURL(url=url, source=self.name,
                                          is_article=self.classify_url(url)))
        return out

    # ── pure requests+lxml BFS fallback (the ONLY path testable here) ───────

    def _bfs_fallback(self, session: requests.Session, keyword: Optional[str],
                       max_urls: int, since: Optional[datetime],
                       result: DiscoveryResult) -> None:
        seed = self._normalize_link(self.seed_url)
        if seed is None:
            result.errors.append(f"{self.domain}: no usable seed_url")
            return

        max_pages = min(self.MAX_PAGES_DEFAULT, max_urls or self.MAX_PAGES_DEFAULT)
        queue: List[Tuple[str, int]] = [(seed, 0)]
        visited: Set[str] = set()
        fetched_pages = 0

        while queue and fetched_pages < max_pages:
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                return
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            resp = self._get_with_retries(session, url, result)
            fetched_pages += 1
            if resp is None:
                continue

            if self.domain == "fmprc.gov.cn" and self._is_fmprc_maintenance_page(resp):
                result.errors.append(
                    f"{self.domain}: {url} redirected to the mfa.gov.cn maintenance "
                    f"page (soft-404 trap); skipped")
                continue

            if not resp.ok:
                result.errors.append(f"{self.domain}: {url} returned HTTP {resp.status_code}")
                continue

            try:
                tree = lxml_html.fromstring(resp.content)
            except (etree.ParserError, ValueError) as exc:
                result.errors.append(f"{self.domain}: failed to parse {url}: {exc!r}")
                continue

            is_article = self.classify_url(url)
            if is_article:
                du = self._make_discovered_url(url, tree)
                if du is not None:
                    result.urls.append(du)
                    if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                        return

            if depth >= self.MAX_DEPTH_DEFAULT:
                continue

            for href in tree.xpath("//a/@href"):
                link = self._normalize_link(urljoin(url, str(href)))
                if link is None or link in visited:
                    continue
                if not self.host_allowed(link):
                    continue
                if keyword and self.classify_url(link) is False:
                    # Skip obvious listing/nav noise when we have a keyword focus --
                    # still allow unknown (None) since that's most article links.
                    continue
                queue.append((link, depth + 1))

            time.sleep(self.crawl_delay())

    # ── fmprc.gov.cn soft-404 detection ──────────────────────────────────────

    @staticmethod
    def _is_fmprc_maintenance_page(resp: requests.Response) -> bool:
        final_url = resp.url or ""
        if _FMPRC_MAINTENANCE_MARKER in final_url:
            return True
        # Secondary signal only: a documented fixed byte length at profiling time.
        # Content may drift, so this alone is not conclusive -- but combined with
        # an unexpected host (mfa.gov.cn instead of fmprc.gov.cn/www.fmprc.gov.cn)
        # it's a strong corroborating signal.
        try:
            content_len = len(resp.content)
        except Exception:
            return False
        host = urlparse(final_url).netloc.lower()
        if "mfa.gov.cn" in host and abs(content_len - _FMPRC_MAINTENANCE_LEN_HINT) < 500:
            return True
        return False

    # ── mizzima.com Cloudflare challenge detection ──────────────────────────

    def _check_cloudflare_challenge(self, session: requests.Session,
                                     result: DiscoveryResult) -> bool:
        """Returns True (and records an error) if the Cloudflare managed challenge
        is detected -- caller should stop rather than burn retries against it."""
        resp = self._get_with_retries(session, self.seed_url, result)
        if resp is None:
            result.errors.append(
                f"{self.domain}: seed unreachable and no response to inspect for a "
                f"Cloudflare challenge; assuming challenged (verified prior state)")
            return True

        header_val = resp.headers.get(_CF_CHALLENGE_HEADER, "")
        if "challenge" in header_val.lower():
            result.errors.append(
                f"{self.domain}: Cloudflare managed challenge detected "
                f"({_CF_CHALLENGE_HEADER}: {header_val!r}); returning empty result "
                f"rather than burning retries against it")
            return True

        if resp.status_code in (403, 503):
            body_sample = ""
            try:
                body_sample = resp.text[:4000]
            except Exception:
                pass
            if any(marker in body_sample for marker in _CF_CHALLENGE_BODY_MARKERS):
                result.errors.append(
                    f"{self.domain}: Cloudflare managed challenge detected "
                    f"(HTTP {resp.status_code} + challenge markers in body); "
                    f"returning empty result rather than burning retries against it")
                return True

        return False

    # ── hornbilltv.com: numeric-ID walk + one-shot keyword topic search ─────

    def _discover_hornbilltv(self, keyword: Optional[str], max_urls: int,
                              since: Optional[datetime], result: DiscoveryResult) -> None:
        session = requests.Session()

        if keyword:
            # Verified: /topics/{kw} returns real hits but pagination is FAKE
            # (?page=2/?page=20 return the identical first page) -- fetch exactly
            # one page, never paginate it.
            self._hornbilltv_topic_search(session, keyword, max_urls, result)
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                return

        self._hornbilltv_id_walk(session, max_urls, since, result)

    def _hornbilltv_topic_search(self, session: requests.Session, keyword: str,
                                  max_urls: int, result: DiscoveryResult) -> None:
        search_cfg = self.profile.get("search") or {}
        template = search_cfg.get("url_template")
        if not template:
            return
        from urllib.parse import quote

        try:
            url = template.format(keyword=quote(keyword, safe=""))
        except (KeyError, IndexError, ValueError):
            result.errors.append(f"{self.domain}: could not build topic-search URL")
            return

        resp = self._get_with_retries(session, url, result)
        if resp is None or not resp.ok:
            if resp is not None:
                result.errors.append(
                    f"{self.domain}: topic search {url} returned HTTP {resp.status_code}")
            return

        try:
            tree = lxml_html.fromstring(resp.content)
        except (etree.ParserError, ValueError) as exc:
            result.errors.append(f"{self.domain}: failed to parse topic search: {exc!r}")
            return

        patterns = self.profile.get("article_url_patterns") or []
        seen: Set[str] = set()
        for a in tree.xpath("//a[@href]"):
            href = str(a.get("href") or "")
            link = self._normalize_link(urljoin(url, href))
            if link is None or link in seen or not self.host_allowed(link):
                continue
            if not any(self._safe_search(p, link) for p in patterns):
                continue
            seen.add(link)
            title = (a.text_content() or "").strip() or None
            try:
                du = DiscoveredURL(url=link, source=self.name, title=title,
                                    is_article=True, metadata={"via": "topics_search"})
            except ValueError:
                continue
            result.urls.append(du)
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                return

    def _hornbilltv_id_walk(self, session: requests.Session, max_urls: int,
                             since: Optional[datetime], result: DiscoveryResult) -> None:
        head_id = self._hornbilltv_head_id()
        walked = 0
        current = head_id
        while walked < _HORNBILLTV_ID_WALK_COUNT and current > 0:
            if max_urls and max_urls > 0 and len(result.urls) >= max_urls:
                return
            url = f"https://www.hornbilltv.com/x/y/{current}"
            resp = self._get_with_retries(session, url, result)
            walked += 1
            current -= 1
            if resp is None:
                continue
            if resp.status_code == 404:
                # IDs are not guaranteed dense (author records share the space per
                # the profile notes) -- a 404 just means skip this one ID, not stop.
                continue
            if not resp.ok:
                continue
            try:
                tree = lxml_html.fromstring(resp.content)
            except (etree.ParserError, ValueError):
                continue

            title = self._first_text(tree, "//h1")
            published_at = self._hornbilltv_jsonld_date(tree)
            if since is not None and published_at is not None and published_at < since:
                # IDs walk newest-to-oldest, so once we're below `since` further
                # older IDs are even less likely to help, but individual IDs are
                # not strictly monotonic in publish time (shared autoincrement
                # across content types per the profile) -- so we skip, not stop.
                continue

            try:
                du = DiscoveredURL(url=url, source=self.name, title=title,
                                    published_at=published_at, is_article=True,
                                    metadata={"via": "id_walk", "id": current + 1})
            except ValueError:
                continue
            result.urls.append(du)
            time.sleep(self.crawl_delay())

    @staticmethod
    def _hornbilltv_jsonld_date(tree) -> Optional[datetime]:
        import json as _json
        for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                data = _json.loads(script)
            except (ValueError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                raw = cand.get("datePublished")
                if not raw or not isinstance(raw, str):
                    continue
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                    try:
                        dt = datetime.strptime(raw, fmt)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt.astimezone(timezone.utc)
                    except ValueError:
                        continue
        return None

    def _hornbilltv_head_id(self) -> int:
        hint = ((self.profile.get("deep_crawl") or {}).get("hornbilltv_head_id"))
        try:
            if hint:
                return int(hint)
        except (TypeError, ValueError):
            pass
        return _HORNBILLTV_VERIFIED_HEAD_ID

    # ── shared helpers ───────────────────────────────────────────────────────

    def _get_with_retries(self, session: requests.Session, url: str,
                           result: DiscoveryResult) -> Optional[requests.Response]:
        final_url = self._rewrite_scheme_if_needed(url)
        attempts = max(1, self.max_retries() + 1)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                resp = session.get(final_url, headers=self.headers(),
                                    timeout=self.timeout(), allow_redirects=True)
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s: attempt %d/%d failed for %s: %r",
                            self.domain, attempt + 1, attempts, final_url, exc)
                if attempt < attempts - 1:
                    time.sleep(self.crawl_delay())
        result.errors.append(
            f"{self.domain}: all retries failed for {final_url}: {last_exc!r}")
        return None

    def _rewrite_scheme_if_needed(self, url: str) -> str:
        """stats.gov.cn: port 80 is firewalled while the site's own internal links
        are written http:// -- rewrite to https:// or every one of those hangs."""
        if self.domain != "stats.gov.cn":
            return url
        transport = self.profile.get("transport") or {}
        if not transport.get("force_https", False):
            return url
        if url.startswith("http://"):
            return "https://" + url[len("http://"):]
        return url

    def _normalize_link(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        url = url.split("#", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            return None
        return self._rewrite_scheme_if_needed(url)

    def _make_discovered_url(self, url: str, tree) -> Optional[DiscoveredURL]:
        title = self._first_text(tree, "//title") or self._first_text(tree, "//h1")
        published_at = self._extract_generic_date(tree)
        try:
            return DiscoveredURL(url=url, source=self.name, title=title,
                                  published_at=published_at,
                                  is_article=self.classify_url(url))
        except ValueError as exc:
            log.warning("%s: skipping malformed URL %r: %r", self.domain, url, exc)
            return None

    @staticmethod
    def _first_text(tree, xpath_expr: str) -> Optional[str]:
        try:
            nodes = tree.xpath(xpath_expr)
        except Exception:
            return None
        for n in nodes:
            text = n.text_content().strip() if hasattr(n, "text_content") else str(n).strip()
            if text:
                return text
        return None

    @staticmethod
    def _extract_generic_date(tree) -> Optional[datetime]:
        # fmprc/stats.gov.cn article slugs embed the date (e.g. t20260731_...html);
        # we don't guess the date from that here to avoid fabricating a value the
        # instructions forbid -- date extraction from a fetched page's own markup
        # (e.g. a <meta> tag) is fine, but we deliberately stay conservative and
        # only trust an explicit, unambiguous machine-readable date field.
        for xp in ('//meta[@property="article:published_time"]/@content',
                   '//meta[@name="publishdate"]/@content',
                   '//time/@datetime'):
            try:
                vals = tree.xpath(xp)
            except Exception:
                continue
            for raw in vals:
                dt = DeepCrawlStrategy._parse_iso(str(raw))
                if dt is not None:
                    return dt
        return None

    @staticmethod
    def _parse_iso(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _safe_search(pattern: str, s: str) -> bool:
        try:
            return bool(re.search(pattern, s))
        except re.error:
            return False
