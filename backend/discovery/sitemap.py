"""
backend/discovery/sitemap.py — SitemapStrategy: generic <urlset>/<sitemapindex> discovery.

Used by dvb.no, pri.gov.np, mod.gov.np (see config/site_profiles.json "strategy": "sitemap").
This module also holds the shared machinery (XML parsing, gzip handling, the crawl4ai
AsyncUrlSeeder bridge, robots.txt caching, a keyword-scoring fallback) that
backend/discovery/news_sitemap.py imports and builds on for Google-News-style sitemaps.

crawl4ai is NOT installed on this machine. Every crawl4ai symbol is imported behind a
try/except exactly like SELENIUM_AVAILABLE / LIGHTPANDA_AVAILABLE in backend/crawler.py,
and every dict crawl4ai hands back is read with .get() only — see the #1306 handling in
entries_from_seeder_results() below. The pure-Python requests+lxml path is not a last
resort here, it is the only path this code has ever actually executed under.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import re
import threading
import time
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

from .base import DiscoveredURL, DiscoveryResult, DiscoveryStrategy, register

log = logging.getLogger("keywordscout.discovery.sitemap")

# ── crawl4ai availability guard (see CRAWL4AI_API.md section 1 and section 4 rule 1) ──
try:
    from crawl4ai import AsyncUrlSeeder, SeedingConfig  # type: ignore
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False


_MAX_CHILD_SITEMAPS = 200        # hard safety cap on sitemapindex recursion breadth
_MAX_PROBE_EXTENSIONS = 120      # hard cap on "probe past the advertised index end"
_TRAILING_NUM_RE = re.compile(r"^(.*?)(\d+)(/?(?:\?.*)?)$")


# ── XML plumbing ───────────────────────────────────────────────────────────────────
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else tag


def looks_gzipped(content: bytes, url: str) -> bool:
    if content[:2] == b"\x1f\x8b":
        return True
    return url.lower().endswith(".gz")


def maybe_gunzip(content: bytes, url: str) -> bytes:
    """Transparently decompress .xml.gz sitemaps (dvb.no's index cross-links some,
    burmese.voanews.com's archive shards are entirely gzipped). Never raises."""
    if not looks_gzipped(content, url):
        return content
    try:
        return gzip.decompress(content)
    except OSError:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
                return gz.read()
        except Exception:
            log.warning("failed to gunzip %s; using raw bytes", url)
            return content


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of <lastmod>/<news:publication_date>/JSON-LD datePublished
    strings to a tz-aware UTC datetime. Never raises; returns None on anything it
    cannot parse rather than fabricating a date."""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        v2 = v[:-1] + "+00:00" if v.endswith("Z") else v
        dt = datetime.fromisoformat(v2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.strptime(v[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class SitemapEntry:
    """One <url> (or <sitemap>) node from a parsed sitemap document."""
    __slots__ = ("loc", "lastmod", "news_pubdate", "news_title")

    def __init__(self, loc: str, lastmod: Optional[str] = None,
                 news_pubdate: Optional[str] = None, news_title: Optional[str] = None):
        self.loc = loc
        self.lastmod = lastmod
        self.news_pubdate = news_pubdate
        self.news_title = news_title


def parse_sitemap(content: bytes) -> Tuple[str, List[SitemapEntry]]:
    """Parse <urlset> or <sitemapindex> XML bytes, namespace-agnostic (handles the
    default sitemap namespace and the <news:...> Google News namespace by local name,
    since real-world feeds use varying namespace prefixes/URIs).

    Returns (kind, entries) where kind is 'sitemapindex', 'urlset', or 'unknown'.
    Never raises — returns ('unknown', []) on any parse failure so a single malformed
    shard cannot take down the whole walk.
    """
    try:
        parser = etree.XMLParser(resolve_entities=False, recover=True, huge_tree=True)
        root = etree.fromstring(content, parser=parser)
    except Exception as exc:
        log.warning("sitemap XML parse failed: %s", exc)
        return "unknown", []
    if root is None:
        return "unknown", []

    root_tag = _strip_ns(root.tag)
    entries: List[SitemapEntry] = []

    if root_tag == "sitemapindex":
        for sm in root:
            if _strip_ns(sm.tag) != "sitemap":
                continue
            loc, lastmod = None, None
            for child in sm:
                ctag = _strip_ns(child.tag)
                if ctag == "loc":
                    loc = (child.text or "").strip()
                elif ctag == "lastmod":
                    lastmod = (child.text or "").strip()
            if loc:
                entries.append(SitemapEntry(loc, lastmod=lastmod))
        return "sitemapindex", entries

    if root_tag == "urlset":
        for u in root:
            if _strip_ns(u.tag) != "url":
                continue
            loc, lastmod, news_pubdate, news_title = None, None, None, None
            for child in u:
                ctag = _strip_ns(child.tag)
                if ctag == "loc":
                    loc = (child.text or "").strip()
                elif ctag == "lastmod":
                    lastmod = (child.text or "").strip()
                elif ctag == "news":
                    for nchild in child:
                        nctag = _strip_ns(nchild.tag)
                        if nctag == "publication_date":
                            news_pubdate = (nchild.text or "").strip()
                        elif nctag == "title":
                            news_title = (nchild.text or "").strip()
            if loc:
                entries.append(SitemapEntry(loc, lastmod=lastmod,
                                             news_pubdate=news_pubdate, news_title=news_title))
        return "urlset", entries

    return "unknown", entries


# ── HTTP fetch ──────────────────────────────────────────────────────────────────────
def fetch_sitemap_bytes(session: requests.Session, url: str, headers: Dict[str, str],
                         timeout: int, max_retries: int) -> Optional[bytes]:
    """GET url and return decompressed bytes, or None on failure. Never raises —
    callers append to DiscoveryResult.errors instead."""
    last_err = None
    for _ in range(max(1, max_retries + 1)):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return maybe_gunzip(resp.content, url)
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        except Exception as exc:  # defensive: a fetch bug must not propagate
            last_err = str(exc)
    log.warning("giving up on %s after %d attempt(s): %s", url, max_retries + 1, last_err)
    return None


def probe_numeric_continuation(session: requests.Session, last_url: str,
                                headers: Dict[str, str], timeout: int, max_retries: int,
                                max_extra: int = _MAX_PROBE_EXTENSIONS):
    """Given a sitemap URL ending in a number (e.g. '.../sitemap/news/99'), keep
    incrementing that number and fetching until a fetch fails or returns zero <url>
    entries, yielding (url, entries) for each successful page found beyond it.

    Exists because kathmandupost.com's /sitemap/ index only advertises 100 children
    (news/1..news/99 + category) but pages 100-161 exist and were found by binary
    search — the index's own child count cannot be trusted as the true end. Bounded
    by max_extra as a safety net: on any site where this pattern doesn't hold, the
    very next probe 404s or comes back empty and this stops immediately.
    """
    m = _TRAILING_NUM_RE.match(last_url)
    if not m:
        return
    prefix, num_str, suffix = m.groups()
    n = int(num_str)
    width = len(num_str)
    for _ in range(max_extra):
        n += 1
        candidate = f"{prefix}{str(n).zfill(width)}{suffix}"
        content = fetch_sitemap_bytes(session, candidate, headers, timeout, max_retries)
        if content is None:
            return
        kind, entries = parse_sitemap(content)
        if kind != "urlset" or not entries:
            return
        yield candidate, entries


def walk_sitemap_tree(
    session: requests.Session,
    seeds: List[str],
    *,
    headers: Dict[str, str],
    timeout: int,
    retries: int,
    delay: float,
    robots_allowed: Callable[[str], bool],
    on_entries: Callable[[List[SitemapEntry]], bool],
    errors: List[str],
    max_child_sitemaps: int = _MAX_CHILD_SITEMAPS,
) -> "set[str]":
    """BFS over a sitemap/sitemapindex tree. Calls on_entries(entries) once per
    <urlset> page found; on_entries returns True to request an early stop (e.g. the
    caller already has max_urls candidates and isn't doing a since-filtered backfill
    where later, older shards still matter).

    Returns the set of sitemap container URLs that were fetched, so callers (e.g.
    kathmandupost's past-the-index-end probing) know where the index left off.
    Never raises: fetch/parse failures are recorded in `errors` and skipped.
    """
    visited: set = set()
    queue: List[str] = list(dict.fromkeys(seeds))
    first = True
    while queue and len(visited) < max_child_sitemaps:
        sm_url = queue.pop(0)
        if sm_url in visited:
            continue
        visited.add(sm_url)
        try:
            allowed = robots_allowed(sm_url)
        except Exception:
            allowed = True
        if not allowed:
            errors.append(f"robots.txt disallows {sm_url}")
            continue
        if not first:
            time.sleep(delay)
        first = False
        content = fetch_sitemap_bytes(session, sm_url, headers, timeout, retries)
        if content is None:
            errors.append(f"failed to fetch {sm_url}")
            continue
        kind, entries = parse_sitemap(content)
        if kind == "sitemapindex":
            for e in entries:
                if e.loc and e.loc not in visited:
                    queue.append(e.loc)
        elif kind == "urlset":
            try:
                stop = on_entries(entries)
            except Exception as exc:
                errors.append(f"on_entries callback failed for {sm_url}: {exc}")
                stop = False
            if stop:
                break
        else:
            errors.append(f"unrecognized sitemap format at {sm_url}")
    return visited


# ── robots.txt (shared, tiny cache) ─────────────────────────────────────────────────
class _RobotsCache:
    _cache: Dict[str, urllib.robotparser.RobotFileParser] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, base_url: str, headers: Dict[str, str], timeout: int
            ) -> urllib.robotparser.RobotFileParser:
        with cls._lock:
            cached = cls._cache.get(base_url)
        if cached is not None:
            return cached
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = requests.get(urljoin(base_url, "/robots.txt"), headers=headers, timeout=timeout)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except Exception:
            rp.parse([])
        with cls._lock:
            cls._cache[base_url] = rp
        return rp


class RobotsAwareMixin:
    """Adds a cached robots.txt check on top of DiscoveryStrategy. Robots.txt is
    fetched per-host (not per self.domain) since sitemaps and articles frequently
    live on a different subdomain than the profile's declared `domain` (e.g. dvb.no's
    profile domain vs. burmese.dvb.no where the sitemap actually lives)."""

    # NOTE: respects_robots(), headers() and timeout() are deliberately NOT redeclared
    # here. This mixin is always combined as `class X(RobotsAwareMixin, DiscoveryStrategy)`,
    # so the mixin sits LEFT of DiscoveryStrategy in the MRO — any method defined here
    # would SHADOW the real DiscoveryStrategy implementation rather than defer to it.
    # (Stub declarations here previously raised NotImplementedError for exactly that
    # reason.) They resolve through the MRO to DiscoveryStrategy at call time.

    def robots_allowed(self, url: str) -> bool:
        if not self.respects_robots():
            return True
        try:
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}/"
            rp = _RobotsCache.get(base, self.headers(), self.timeout())
            return rp.can_fetch(self.headers().get("User-Agent", "*"), url)
        except Exception:
            # Fail open: a broken robots.txt fetch must not silently stop discovery.
            return True


# ── crawl4ai bridge ──────────────────────────────────────────────────────────────────
def run_async(coro):
    """Run an async crawl4ai coroutine to completion from discover()'s synchronous
    contract. asyncio.run() raises if called from inside an already-running event
    loop (e.g. if discover() is ever invoked from async code, or from a worker that
    itself owns a loop) — detect that case and run the coroutine in a dedicated
    thread with its own fresh loop instead, so this never explodes inside the
    existing ThreadPoolExecutor-based pipeline.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - re-raised on the caller's thread
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def seed_via_crawl4ai(domain: str, pattern: str, keyword: Optional[str],
                       max_urls: int, hits_per_sec: float) -> Optional[List[Dict[str, Any]]]:
    """Run crawl4ai's AsyncUrlSeeder synchronously for `domain` (a BARE domain string,
    per CRAWL4AI_API.md — NOT a full URL). Returns None (never []) when crawl4ai is
    unavailable or the call raises for any reason, so callers can tell "not
    available/failed, fall back" apart from "ran fine and legitimately found nothing".
    """
    if not CRAWL4AI_AVAILABLE:
        return None
    try:
        cfg_kwargs: Dict[str, Any] = dict(
            source="sitemap",
            pattern=pattern or "*",
            extract_head=True,
            live_check=False,
            max_urls=max_urls if max_urls and max_urls > 0 else -1,
            concurrency=10,
            hits_per_sec=hits_per_sec,
            force=False,
            filter_nonsense_urls=True,
            cache_ttl_hours=24,
            validate_sitemap_lastmod=True,
            verbose=False,
        )
        if keyword:
            cfg_kwargs.update(query=keyword, scoring_method="bm25", score_threshold=0.0)
        cfg = SeedingConfig(**cfg_kwargs)

        async def _run() -> List[Dict[str, Any]]:
            async with AsyncUrlSeeder() as seeder:
                res = await seeder.urls(domain, cfg)
                return res or []

        return run_async(_run())
    except Exception as exc:
        log.warning("crawl4ai AsyncUrlSeeder failed for %s: %s", domain, exc)
        return None


def _keyword_score(text: str, keyword: str) -> float:
    """Trivial term-frequency fallback scorer, used both when crawl4ai is absent and
    when crawl4ai's BM25 scoring comes back empty for every result (issue #1306)."""
    if not keyword:
        return 0.0
    text_l = (text or "").lower()
    terms = [t for t in re.split(r"\W+", keyword.lower()) if t]
    if not terms:
        return 0.0
    hits = sum(text_l.count(t) for t in terms)
    return hits / len(terms)


def _keyword_score_or_none(text: str, url: str, keyword: Optional[str]) -> Optional[float]:
    if not keyword:
        return None
    return _keyword_score(f"{text} {url}", keyword)


def _fallback_keyword_score(head_data: Dict[str, Any], url: str, keyword: str) -> float:
    head_data = head_data if isinstance(head_data, dict) else {}
    title = head_data.get("title") or ""
    meta = head_data.get("meta")
    desc = meta.get("description") if isinstance(meta, dict) else None
    return _keyword_score(f"{title} {desc or ''} {url}", keyword)


def entries_from_seeder_results(
    raw: List[Dict[str, Any]],
    *,
    source_name: str,
    keyword: Optional[str],
    host_allowed: Callable[[str], bool],
    classify_url: Callable[[str], Optional[bool]],
    scope_prefix: Optional[str],
    since: Optional[datetime],
) -> List[DiscoveredURL]:
    """Convert AsyncUrlSeeder's raw List[Dict] into DiscoveredURL, defensively.

    MANDATORY per the documented crawl4ai issue #1306 (still open as of 0.9.2):
    every key on every dict — top-level and nested (head_data, meta, jsonld) — is
    read with .get(), never []. If BM25 scoring was requested but came back missing
    for every single result, log a warning and fall back to our own keyword scorer
    instead of returning an empty list.
    """
    if not raw:
        return []

    scores = [d.get("relevance_score") for d in raw if isinstance(d, dict)]
    bm25_all_missing = bool(keyword) and bool(raw) and all(s is None for s in scores)
    if bm25_all_missing:
        log.warning(
            "crawl4ai BM25 scoring returned no relevance_score for any of %d result(s) "
            "(see crawl4ai issue #1306); falling back to a keyword scorer over "
            "title + meta description + URL path instead of dropping the batch",
            len(raw))

    out: List[DiscoveredURL] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        url = d.get("url")
        if not url:
            continue
        if d.get("status") == "not_valid":
            continue
        try:
            if not host_allowed(url):
                continue
        except Exception:
            continue
        if scope_prefix:
            try:
                if not urlparse(url).path.startswith(scope_prefix):
                    continue
            except Exception:
                continue
        is_article = classify_url(url)
        if is_article is False:
            continue

        head_data = d.get("head_data")
        head_data = head_data if isinstance(head_data, dict) else {}
        title = head_data.get("title")

        published_at = None
        for item in (head_data.get("jsonld") or []):
            if isinstance(item, dict) and item.get("datePublished"):
                published_at = parse_datetime(item.get("datePublished"))
                if published_at:
                    break
        if since and published_at and published_at < since:
            continue

        score = d.get("relevance_score")
        if score is None and keyword:
            score = _fallback_keyword_score(head_data, url, keyword)

        try:
            out.append(DiscoveredURL(
                url=url, source=source_name, title=title, published_at=published_at,
                relevance_score=score, is_article=is_article,
                metadata={"via": "crawl4ai_seeder", "status": d.get("status")},
            ))
        except ValueError:
            continue  # defensive: DiscoveredURL rejects non-http(s) urls
    return out


# ── strategy ─────────────────────────────────────────────────────────────────────────
@register
class SitemapStrategy(RobotsAwareMixin, DiscoveryStrategy):
    """Generic <urlset>/<sitemapindex> discovery driven by the profile's `sitemaps`
    list. Used by dvb.no (filters ~55k tag/page URLs mixed into shards 0/15-20 via
    classify_url + listing_url_patterns), pri.gov.np, and mod.gov.np.

    Tries crawl4ai's AsyncUrlSeeder first; falls back to a pure-Python requests+lxml
    walk when crawl4ai is unavailable or errors. Since crawl4ai is not installed on
    this machine, the fallback path is the only one that has actually been run.
    """
    name = "sitemap"

    def discover(self, keyword: Optional[str] = None, max_urls: int = 500,
                 since: Optional[datetime] = None) -> DiscoveryResult:
        result = DiscoveryResult(strategy=self.name, domain=self.domain)
        try:
            urls: List[DiscoveredURL] = []
            if CRAWL4AI_AVAILABLE:
                try:
                    delay = self.crawl_delay()
                    hits_per_sec = (1.0 / delay) if delay > 0 else 5.0
                    raw = seed_via_crawl4ai(self.domain, "*", keyword, max_urls, hits_per_sec)
                    if raw:
                        urls = entries_from_seeder_results(
                            raw, source_name=self.name, keyword=keyword,
                            host_allowed=self.host_allowed, classify_url=self.classify_url,
                            scope_prefix=None, since=since)
                except Exception as exc:
                    log.warning("%s: crawl4ai seeding failed (%s); falling back to XML walk",
                                self.domain, exc)
                    result.errors.append(f"crawl4ai path failed: {exc!r}")
            if not urls:
                urls = self._discover_via_xml(keyword, max_urls, since, result)
            result.urls = self._cap(urls, max_urls, result)
        except Exception as exc:
            log.exception("%s: sitemap discover() failed", self.domain)
            result.errors.append(f"discover() failed: {exc!r}")
        return result

    def _seed_urls(self) -> List[str]:
        seeds = [e.get("url") for e in (self.profile.get("sitemaps") or []) if e.get("url")]
        seeds = list(dict.fromkeys(seeds))
        if not seeds:
            derived = f"https://{self.domain}/sitemap.xml"
            log.warning("%s: no sitemaps declared in profile; guessing %s", self.domain, derived)
            seeds = [derived]
        return seeds

    def _discover_via_xml(self, keyword: Optional[str], max_urls: int,
                           since: Optional[datetime], result: DiscoveryResult
                           ) -> List[DiscoveredURL]:
        session = requests.Session()
        headers, timeout = self.headers(), self.timeout()
        retries, delay = self.max_retries(), self.crawl_delay()
        seen_urls: set = set()
        discovered: List[DiscoveredURL] = []
        overcollect_cap = max(max_urls * 5, 5000) if max_urls and max_urls > 0 else 50000

        def on_entries(entries: List[SitemapEntry]) -> bool:
            for e in entries:
                if len(discovered) >= overcollect_cap:
                    return True
                url = e.loc
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                if not self.host_allowed(url):
                    continue
                is_article = self.classify_url(url)
                if is_article is False:
                    continue
                published_at = parse_datetime(e.news_pubdate) or parse_datetime(e.lastmod)
                if since and published_at and published_at < since:
                    continue
                score = _keyword_score_or_none(e.news_title or "", url, keyword)
                discovered.append(DiscoveredURL(
                    url=url, source=self.name, title=e.news_title, published_at=published_at,
                    relevance_score=score, is_article=is_article,
                    metadata={"lastmod": e.lastmod, "via": "xml"}))
            # Early-exit heuristic only when we're not doing a since-filtered backfill:
            # profiles list sitemaps in a stable order, so once we have enough
            # candidates further shards can wait for the next poll.
            return bool(max_urls and max_urls > 0 and len(discovered) >= max_urls and since is None)

        walk_sitemap_tree(session, self._seed_urls(), headers=headers, timeout=timeout,
                           retries=retries, delay=delay, robots_allowed=self.robots_allowed,
                           on_entries=on_entries, errors=result.errors)
        return discovered
