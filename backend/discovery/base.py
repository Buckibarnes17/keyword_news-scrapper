"""
backend/discovery/base.py — contract for per-site discovery strategies.

WHY THIS EXISTS
---------------
The previous pipeline applied one discovery mechanism to every site: fetch the
landing page, take up to 100 same-domain <a href>, stop (see the old
queue_manager.run_direct_discovery). Live probing of all 25 sources on 2026-08-01
showed that the correct mechanism differs per site and that for several sites the
obvious route is a decoy which returns HTTP 200 while serving nothing useful:

  - chinadaily.com.cn  sitemap frozen at 2014, RSS frozen at 2017 (both HTTP 200)
  - cgtn.com           articles live on newsaf/newseu/newsus, NOT www; www 404s
                       with a 116 KB page that passes naive validity checks
  - fmprc.gov.cn       /en/ 302s to a maintenance page; every unknown path does too
  - voanews (burmese)  all ~60 RSS feeds stale since 2025-04 while sitemaps are current

So each site declares its own strategy in config/site_profiles.json, and this module
defines the single interface those strategies implement.

CONTRACT
--------
Every strategy is a subclass of DiscoveryStrategy implementing discover(). It returns
an iterable of DiscoveredURL. It MUST NOT write to the database, mutate the profile,
or raise on network failure — return what was found and record the rest in .errors.
"""
from __future__ import annotations

import re
import fnmatch
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

log = logging.getLogger("keywordscout.discovery")

# Full browser header set. Verified empirically: burma.irrawaddy.com returns 403 with a
# User-Agent alone but 200 with these headers present. Do not trim this set.
BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


@dataclass
class DiscoveredURL:
    """One candidate URL plus whatever metadata was obtained without fetching the page.

    Populating `published_at`, `title` and `relevance_score` here is the entire point of
    the redesign: it lets the caller drop irrelevant or stale URLs BEFORE spending a
    fetch, which is what the old pipeline could not do.
    """
    url: str
    source: str                                   # strategy name that produced it
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    relevance_score: Optional[float] = None       # may be None; never assume present
    is_article: Optional[bool] = None             # None = unknown, decide downstream
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url or not self.url.startswith(("http://", "https://")):
            raise ValueError(f"DiscoveredURL requires an absolute http(s) URL, got {self.url!r}")


@dataclass
class DiscoveryResult:
    """Return value of DiscoveryStrategy.discover()."""
    urls: List[DiscoveredURL] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    strategy: str = ""
    domain: str = ""
    truncated: bool = False                       # True if max_urls capped the result

    def __len__(self) -> int:
        return len(self.urls)


class DiscoveryStrategy(ABC):
    """Base class for all discovery strategies.

    Subclasses set `name` and implement `discover`. Construction must stay cheap and
    side-effect free — all network I/O belongs in discover().
    """

    name: str = "base"

    def __init__(self, profile: Dict[str, Any]):
        self.profile = profile
        self.domain: str = profile.get("domain", "")
        self.seed_url: str = profile.get("seed_url") or f"https://{self.domain}/"

    # ── required ────────────────────────────────────────────────────────────
    @abstractmethod
    def discover(self, keyword: Optional[str] = None, max_urls: int = 500,
                 since: Optional[datetime] = None) -> DiscoveryResult:
        """Return candidate URLs for this site.

        keyword  — optional; when supported natively (WP ?search=, site search, BM25)
                   the strategy SHOULD push it server-side rather than filtering later.
        max_urls — hard cap; set DiscoveryResult.truncated when it bites.
        since    — only return items published at/after this instant when the source
                   exposes a date. Never fabricate a date to satisfy this filter.

        MUST NOT raise on network failure. Append to result.errors and return partial
        results — a strategy that raises takes down discovery for the whole site.
        """
        raise NotImplementedError

    # ── shared helpers ──────────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return bool(self.profile.get("enabled", True))

    def respects_robots(self) -> bool:
        """False only where an operator recorded an explicit, reasoned per-site override."""
        return bool((self.profile.get("robots") or {}).get("respect", True))

    def crawl_delay(self) -> float:
        """Site-declared Crawl-delay, else the 1.0s global default.
        Verified values: dvb.no 2s, irrawaddy.com 10s. Honour them — aggressive probing
        during profiling tripped an nginx ban on mmbiztoday.com."""
        d = (self.profile.get("robots") or {}).get("crawl_delay")
        try:
            return float(d) if d is not None else 1.0
        except (TypeError, ValueError):
            return 1.0

    def headers(self) -> Dict[str, str]:
        return dict(BROWSER_HEADERS)

    def timeout(self) -> int:
        return int((self.profile.get("transport") or {}).get("timeout_s", 25))

    def max_retries(self) -> int:
        return int((self.profile.get("transport") or {}).get("max_retries", 2))

    def host_allowed(self, url: str) -> bool:
        """Scope check. Defaults to same registrable host, but honours allowed_hosts —
        cgtn.com needs '*.cgtn.com' because articles never live on www."""
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return False
        if not host:
            return False
        if host.startswith("www."):
            host_bare = host[4:]
        else:
            host_bare = host
        patterns = self.profile.get("allowed_hosts")
        if patterns:
            return any(fnmatch.fnmatch(host, p) or fnmatch.fnmatch(host_bare, p)
                       for p in patterns)
        return host_bare == self.domain or host_bare.endswith("." + self.domain)

    def classify_url(self, url: str) -> Optional[bool]:
        """True=article, False=listing/nav, None=unknown, using the profile's regexes.

        Listing patterns win over article patterns. This is what stops ifa.gov.np from
        reporting 36 'articles' that were really Events/Press-Release/staff-bio pages.
        """
        for pat in self.profile.get("listing_url_patterns") or []:
            try:
                if re.search(pat, url):
                    return False
            except re.error:
                log.warning("bad listing regex for %s: %r", self.domain, pat)
        for pat in self.profile.get("article_url_patterns") or []:
            try:
                if re.search(pat, url):
                    return True
            except re.error:
                log.warning("bad article regex for %s: %r", self.domain, pat)
        return None

    def _cap(self, urls: List[DiscoveredURL], max_urls: int,
             result: DiscoveryResult) -> List[DiscoveredURL]:
        if max_urls and max_urls > 0 and len(urls) > max_urls:
            result.truncated = True
            return urls[:max_urls]
        return urls


# ── registry ────────────────────────────────────────────────────────────────
_REGISTRY: Dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator. Registers a strategy under its `name`."""
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ValueError(f"{cls.__name__} must define a unique `name`")
    if name in _REGISTRY:
        raise ValueError(f"duplicate discovery strategy name {name!r}")
    _REGISTRY[name] = cls
    return cls


def get_strategy(profile: Dict[str, Any]) -> Optional[DiscoveryStrategy]:
    """Instantiate the strategy a profile declares. None if disabled or unregistered."""
    if not profile.get("enabled", True):
        log.info("site %s disabled: %s", profile.get("domain"),
                 profile.get("disabled_reason", "no reason recorded"))
        return None
    name = profile.get("strategy")
    cls = _REGISTRY.get(name)
    if cls is None:
        log.error("no discovery strategy registered under %r (site %s); "
                  "registered: %s", name, profile.get("domain"), sorted(_REGISTRY))
        return None
    return cls(profile)


def registered_strategies() -> List[str]:
    return sorted(_REGISTRY)
