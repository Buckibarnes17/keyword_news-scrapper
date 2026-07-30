"""
url_classifier.py
URL content-type classifier for KeywordScout v2.0.

Filters non-content URLs (search result pages, paginated search pages,
no-results pages) from candidate_urls before they enter the crawl pipeline.

This module is PURELY a filter — no network I/O, no state mutation.
All classification is done on the URL string alone. O(1) per URL.

Dependencies: re, urllib.parse (stdlib only). No new dependencies.
"""
import os
import json
import re
import urllib.parse
from typing import List

# Query parameter names that signal a search query is in the URL
_SEARCH_QUERY_PARAMS = frozenset({
    "q", "query", "search", "s", "keyword", "keywords", "k",
    "keys", "searchword", "search_api_views_fulltext",
    "text", "term", "find", "sr", "search_query", "search_keyword",
    "buscar", "suche",
})

# Path segments that indicate a search endpoint
_SEARCH_PATH_RE = re.compile(
    r'(/search(?:/|$|\?))'
    r'|(/find(?:/|$|\?))'
    r'|(/results(?:/|$|\?))'
    r'|(/searchresults(?:/|$|\?))'
    r'|(/search-results(?:/|$|\?))'
    r'|(/site-search(?:/|$|\?))'
    r'|(/query(?:/|$|\?))',
    re.IGNORECASE
)

# WordPress path pagination pattern
_WP_PATH_PAGINATION_RE = re.compile(r'/page/\d+/?', re.IGNORECASE)

# Search query param in query string
_SEARCH_PARAM_IN_QUERY_RE = re.compile(
    r'[?&](q|s|query|search|keyword|keywords|find|keys|searchword|term)=',
    re.IGNORECASE
)

# "No results" signals anywhere in the URL
_NO_RESULTS_RE = re.compile(
    r'no[_-]?results|0[_-]?results|not[_-]?found|noresult',
    re.IGNORECASE
)


def _has_search_query_param(parsed: urllib.parse.ParseResult) -> bool:
    """Returns True if the URL has a recognized search query parameter NAME."""
    if not parsed.query:
        return False
    for param_name, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if param_name.lower() in _SEARCH_QUERY_PARAMS:
            return True
    return False


def is_search_result_page(url: str) -> bool:
    """
    Returns True if the URL is a search result listing page, paginated search
    page, or no-results page. Returns False for all regular content URLs.

    Conservative: when in doubt, returns False (let the URL through).
    A false negative (letting a search page through) is less damaging than a
    false positive (blocking a legitimate article).
    """
    if not url or not url.startswith("http"):
        return False

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    # Rule 1: Path matches a known search endpoint
    if _SEARCH_PATH_RE.search(parsed.path):
        return True

    # Rule 2: Has a recognized search query parameter name
    if _has_search_query_param(parsed):
        return True

    # Rule 3: WordPress paginated search — /page/N/ in path + search param in query
    # (Pagination alone is NOT filtered — only pagination + search together)
    if _WP_PATH_PAGINATION_RE.search(parsed.path) and _SEARCH_PARAM_IN_QUERY_RE.search('?' + parsed.query):
        return True

    # Rule 4: "No results" signal anywhere in the URL
    if _NO_RESULTS_RE.search(url):
        return True

    return False


def filter_candidate_urls(urls: List[str]) -> List[str]:
    """
    Removes search result pages and pagination pages from a candidate URL list.
    Returns only content-eligible URLs. Preserves order. Does not deduplicate.

    Args:
        urls: List of absolute URL strings (already deduplicated by caller).

    Returns:
        Filtered list with search/pagination/no-results pages removed.
    """
    filtered = []
    removed = []

    for url in urls:
        if is_search_result_page(url):
            removed.append(url)
        else:
            filtered.append(url)

    if removed:
        print(f"[URLClassifier] Removed {len(removed)} search/pagination URLs. "
              f"{len(filtered)} content URLs remain.")
        for u in removed:
            print(f"[URLClassifier]   x {u}")

    return filtered


# ── Chinese Sources / Region Classifier ───────────────────────────────────────────

_CHINESE_SOURCES_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "chinese_sources.json"
)

_URLS_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "urls.json"
)

_chinese_domains = set()
_chinese_tlds = set()
_chinese_sources_loaded = False

def load_chinese_sources(force: bool = False):
    """Loads/reloads Chinese source domains and TLDs from config files."""
    global _chinese_domains, _chinese_tlds, _chinese_sources_loaded
    if _chinese_sources_loaded and not force:
        return

    # 1. Load defaults or from config/chinese_sources.json
    if os.path.exists(_CHINESE_SOURCES_CONFIG_PATH):
        try:
            with open(_CHINESE_SOURCES_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                _chinese_domains = {d.lower().strip() for d in config.get("domains", []) if d.strip()}
                _chinese_tlds = {t.lower().strip() for t in config.get("tlds", []) if t.strip()}
        except Exception as e:
            print(f"[url_classifier] Error loading chinese_sources.json: {e}")
    
    if not _chinese_domains:
        _chinese_domains = {
            "chinadaily.com.cn",
            "cgtn.com",
            "globaltimes.cn",
            "fmprc.gov.cn",
            "stats.gov.cn"
        }
    if not _chinese_tlds:
        _chinese_tlds = {".cn"}

    # 2. Dynamically scan config/urls.json for any entry with group == "china"
    if os.path.exists(_URLS_CONFIG_PATH):
        try:
            with open(_URLS_CONFIG_PATH, "r", encoding="utf-8") as f:
                urls_data = json.load(f)
                for entry in urls_data.get("urls", []):
                    if entry.get("group") == "china" and entry.get("url"):
                        u_parsed = urllib.parse.urlparse(entry["url"])
                        u_netloc = u_parsed.netloc.lower()
                        if u_netloc.startswith("www."):
                            u_netloc = u_netloc[4:]
                        if u_netloc:
                            _chinese_domains.add(u_netloc)
        except Exception as e:
            print(f"[url_classifier] Error loading urls.json for Chinese domains: {e}")

    _chinese_sources_loaded = True


def is_chinese_url(url: str) -> bool:
    """
    Returns True if the URL is classified as Chinese based on:
    1. Suffix/match against domains in the config-driven list.
    2. Ends with a target TLD (e.g. '.cn').
    """
    if not url or not url.startswith("http"):
        return False
    
    load_chinese_sources()
    
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        
        # Direct check or subdomain check
        if netloc in _chinese_domains:
            return True
            
        for cd in _chinese_domains:
            if netloc.endswith("." + cd):
                return True
                
        # TLD check
        for tld in _chinese_tlds:
            if netloc.endswith(tld) or f"{tld}." in netloc or f"{tld}/" in url:
                if netloc == tld.lstrip('.') or netloc.endswith(tld):
                    return True
    except Exception:
        pass
    return False
