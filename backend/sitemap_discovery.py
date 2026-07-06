"""
sitemap_discovery.py
Sitemap and RSS/Atom feed URL discovery engine.
Implements Trafilatura-style sitemap guessing + XML parsing.
Pure Python with stdlib + requests + BeautifulSoup (no new dependencies).
Additive to KeywordScout v2.0.

Dependencies:
    - requests
    - bs4 (BeautifulSoup)
    - urllib (stdlib)
    - re (stdlib)
    - logging (stdlib)
"""

import re
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup
from typing import List, Optional

LOGGER = logging.getLogger(__name__)

SITEMAP_GUESSES = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap/sitemap.xml", "/news-sitemap.xml", "/sitemaps/sitemap.xml",
    "/sitemap1.xml", "/post-sitemap.xml", "/page-sitemap.xml",
]

FEED_GUESSES = [
    "/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
    "/feeds/posts/default", "/index.xml",
]

SITEMAP_LINK_RE = re.compile(r"<loc>(?:<!\[CDATA\[)?(https?://[^<]+?)(?:\]\]>)?</loc>", re.IGNORECASE)
URL_IN_TEXT_RE = re.compile(r"https?://[^\s<\"']+")

def _get_with_ua(url: str, timeout: int = 10) -> Optional[requests.Response]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KeywordScout/2.0; +https://github.com/Viswajith06)",
        "Accept": "text/html,application/xml,application/xhtml+xml,text/xml,*/*;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
    except Exception as e:
        LOGGER.debug(f"[SitemapDiscovery] Request failed for {url}: {e}")
    return None

def _extract_urls_from_xml(text: str) -> List[str]:
    """Extract all <loc>URLs</loc> from a sitemap XML string."""
    return SITEMAP_LINK_RE.findall(text)

def _is_sitemap(text: str) -> bool:
    return bool(re.search(r"<\?xml|<sitemap|<urlset|<sitemapindex", text[:2000], re.IGNORECASE))

def _is_feed(text: str, content_type: str = "") -> bool:
    ct = content_type.lower()
    return (
        "rss" in ct or "atom" in ct or "feed" in ct or
        bool(re.search(r"<rss|<feed|<channel", text[:1000], re.IGNORECASE))
    )

def discover_from_sitemap(base_url: str, max_urls: int = 500) -> List[str]:
    """
    Discovers crawlable URLs from a domain's sitemap(s).
    Handles sitemap index files (nested sitemaps), plain XML sitemaps, and TXT sitemaps.
    Returns a deduplicated list of absolute page URLs, capped at max_urls.
    """
    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    
    discovered: list[str] = []
    seen: set[str] = set()
    sitemap_queue: list[str] = []

    # 1. Check robots.txt for sitemap declarations
    robots_url = f"{origin}/robots.txt"
    robots_resp = _get_with_ua(robots_url)
    if robots_resp:
        for line in robots_resp.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if sm_url not in seen:
                    sitemap_queue.append(sm_url)
                    seen.add(sm_url)

    # 2. Try standard guess paths if robots.txt gave nothing
    if not sitemap_queue:
        for guess in SITEMAP_GUESSES:
            sitemap_queue.append(origin + guess)

    # 3. Process sitemap queue
    processed_sitemaps: set[str] = set()
    while sitemap_queue and len(discovered) < max_urls:
        sm_url = sitemap_queue.pop(0)
        if sm_url in processed_sitemaps:
            continue
        processed_sitemaps.add(sm_url)

        resp = _get_with_ua(sm_url)
        if not resp:
            continue

        content = resp.text
        
        # TXT sitemaps (one URL per line)
        if sm_url.endswith(".txt") or (not _is_sitemap(content) and "http" in content):
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("http") and line not in seen:
                    discovered.append(line)
                    seen.add(line)
            continue

        # XML sitemap
        if _is_sitemap(content):
            urls = _extract_urls_from_xml(content)
            for url in urls:
                url = url.strip()
                if not url:
                    continue
                # Nested sitemap index: if the URL points to another .xml, queue it
                if url.endswith(".xml") or "sitemap" in url.lower():
                    if url not in processed_sitemaps and url not in sitemap_queue:
                        sitemap_queue.append(url)
                elif url not in seen:
                    discovered.append(url)
                    seen.add(url)

    return discovered[:max_urls]

def discover_from_feeds(base_url: str, max_urls: int = 200) -> List[str]:
    """
    Discovers article URLs from RSS/Atom/JSON feeds.
    Returns a list of absolute URLs found in feed <link> elements.
    """
    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    discovered: list[str] = []
    seen: set[str] = set()

    feed_candidates = [base_url] + [origin + g for g in FEED_GUESSES]

    for feed_url in feed_candidates:
        if len(discovered) >= max_urls:
            break
        resp = _get_with_ua(feed_url)
        if not resp:
            continue
        
        ct = resp.headers.get("Content-Type", "")
        if not _is_feed(resp.text, ct):
            continue

        # Parse as XML
        try:
            soup = BeautifulSoup(resp.text, "xml")
            # RSS: <link> inside <item>
            for item in soup.find_all("item"):
                link = item.find("link")
                if link and link.get_text():
                    url = link.get_text().strip()
                    if url.startswith("http") and url not in seen:
                        discovered.append(url)
                        seen.add(url)
            # Atom: <link href="..."> inside <entry>
            for entry in soup.find_all("entry"):
                link = entry.find("link")
                if link and link.get("href"):
                    url = link.get("href").strip()
                    if url.startswith("http") and url not in seen:
                        discovered.append(url)
                        seen.add(url)
        except Exception:
            # Fallback: regex scrape for URLs in feed body
            for url in URL_IN_TEXT_RE.findall(resp.text):
                if url.startswith("http") and url not in seen:
                    discovered.append(url)
                    seen.add(url)

    return discovered[:max_urls]
