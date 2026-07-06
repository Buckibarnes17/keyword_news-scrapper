"""
url_classifier.py
URL content-type classifier for KeywordScout v2.0.

Filters non-content URLs (search result pages, paginated search pages,
no-results pages) from candidate_urls before they enter the crawl pipeline.

This module is PURELY a filter — no network I/O, no state mutation.
All classification is done on the URL string alone. O(1) per URL.

Dependencies: re, urllib.parse (stdlib only). No new dependencies.
"""

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
