"""
site_search_detector.py
Site-native search detection and URL discovery engine for KeywordScout v2.0.

Detects whether a submitted URL's website exposes a native search feature,
constructs a search request using that feature, and returns discovered URLs.

This module is ADDITIVE to the existing crawl pipeline. It must never raise
exceptions to its caller — all failures return None, letting the caller fall
through to the existing crawl logic.

Dependencies: bs4 (already in requirements), urllib.parse (stdlib).
No new dependencies required.

## Changes
- Added _is_search_url() helper to identify search result pages and WordPress
  paginated search URLs (/page/N/?s=query pattern).
- Added _is_search_url() guard inside _extract_result_urls() to prevent search
  result listing pages from being returned as content URLs.
- Added _PATH_PAGINATION_RE and _SEARCH_PARAM_RE compiled patterns.
"""

import os
import re
import urllib.parse
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup

# ── Search signal patterns ────────────────────────────────────────────────────

# Matches input name/id/placeholder attributes indicating a search field
_SEARCH_ATTR_RE = re.compile(
    r'\b(search|query|q|find|keyword|keywords|s|term|terms|text|buscar|suche)\b',
    re.IGNORECASE
)

# Matches form action URLs indicating a search endpoint
_SEARCH_ACTION_RE = re.compile(
    r'(search|find|query|results|buscar|suche|zoeken|/?s=|/?q=)',
    re.IGNORECASE
)

# URL patterns that indicate pagination in search results
_PAGINATION_RE = re.compile(
    r'[?&](page|p|pg|paged|start|offset|from)=\d+',
    re.IGNORECASE
)

# Matches WordPress-style path-based pagination: /page/2/, /page/3/, etc.
# Combined with a search query param, this identifies paginated search results.
_PATH_PAGINATION_RE = re.compile(
    r'/page/\d+/?',
    re.IGNORECASE
)

# Matches query parameters that indicate a search query is in the URL.
# Used to distinguish search-result pagination from archive pagination.
_SEARCH_PARAM_RE = re.compile(
    r'[?&](q|s|query|search|keyword|keywords|find|keys|searchword|term)=',
    re.IGNORECASE
)

# File extensions to exclude from discovered result URLs
_SKIP_EXTENSIONS = {
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp',
    '.zip', '.tar', '.gz', '.css', '.js', '.xml', '.json',
    '.mp4', '.mp3', '.wav', '.avi', '.mov', '.ico', '.woff',
    '.woff2', '.ttf', '.eot'
}

# Maximum URLs to return per site-search discovery run
MAX_DISCOVERY_URLS = 200

# Maximum additional pages to follow during pagination
MAX_PAGINATION_PAGES = 5


def _get_origin(url: str) -> str:
    """Returns scheme://netloc for a URL."""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_domain(url: str) -> str:
    """Returns the bare domain (no www.) for a URL."""
    netloc = urllib.parse.urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _is_same_domain(url: str, origin_domain: str) -> bool:
    """Returns True if a URL belongs to the same domain."""
    return _get_domain(url) == origin_domain


def _is_skippable_url(url: str) -> bool:
    """Returns True if a URL should be excluded from results (static assets, anchors)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.fragment and not parsed.path:
        return True
    ext = os.path.splitext(parsed.path)[1].lower()
    return ext in _SKIP_EXTENSIONS


def _is_navigation_url(url: str, base_url: str) -> bool:
    """
    Heuristic: returns True if a URL looks like navigation/pagination
    rather than a content result.
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip('/')
    # Very short paths (home, /about, /contact) are likely navigation
    segments = [s for s in path.split('/') if s]
    if len(segments) == 0:
        return True
    # Pagination params in URL
    if _PAGINATION_RE.search(url):
        return True  # Pagination links are NOT content, they are navigation
    return False


def _is_search_url(url: str) -> bool:
    """
    Returns True if the given URL is a search result listing page or
    a paginated search results page — NOT a content article.

    Two patterns are caught:
    1. URL path contains a known search endpoint segment (/search, /find, /results).
    2. URL has BOTH a path-based pagination pattern (/page/N/) AND a search query
       parameter (?s=, ?q=, etc.) — the WordPress paginated search pattern.

    This is intentionally conservative: pagination alone (/page/2/) is NOT filtered
    because paginated article archives are valid content sources.
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    query = parsed.query

    # Rule 1: Path is a search endpoint
    search_path_segments = ('/search', '/find/', '/results', '/site-search', '/query')
    for seg in search_path_segments:
        if seg in path:
            return True

    # Rule 2: WordPress paginated search — /page/N/ + search query param
    if _PATH_PAGINATION_RE.search(path) and _SEARCH_PARAM_RE.search('?' + query):
        return True

    # Rule 3: Query string alone contains a search param (covers ?s=, ?q=, ?search=)
    if _SEARCH_PARAM_RE.search('?' + query):
        return True

    return False


def _detect_search_form(soup: BeautifulSoup) -> Optional[Tuple[str, str, dict]]:
    """
    Searches the parsed HTML for a site search form.

    Returns: (action_url, input_name, hidden_fields_dict) if found, else None.

    Strategy:
    1. Find all <form> elements.
    2. Score each form by its inputs for search signals.
    3. Return the highest-scored form's action + search input name + hidden fields.
    """
    best_form = None
    best_input_name = None
    best_score = 0

    for form in soup.find_all("form"):
        form_action = form.get("action", "")
        form_score = 0
        search_input = None

        # Score the form action URL
        if form_action and _SEARCH_ACTION_RE.search(form_action):
            form_score += 3

        # Score the form's method (GET is required for search)
        method = (form.get("method") or "get").lower()
        if method == "post":
            form_score -= 2  # POST forms are usually not site search

        # Score form role/class/id attributes
        for attr in ("role", "class", "id", "aria-label"):
            val = form.get(attr, "")
            if isinstance(val, list):
                val = " ".join(val)
            if _SEARCH_ATTR_RE.search(str(val)):
                form_score += 2

        # Score inputs inside the form
        for inp in form.find_all("input"):
            inp_type = (inp.get("type") or "text").lower()
            inp_name = inp.get("name", "")
            inp_id = inp.get("id", "")
            inp_placeholder = inp.get("placeholder", "")

            if inp_type == "search":
                form_score += 5
                if not search_input:
                    search_input = inp
            elif inp_type in ("text", ""):
                for attr_val in (inp_name, inp_id, inp_placeholder):
                    if _SEARCH_ATTR_RE.search(str(attr_val)):
                        form_score += 3
                        if not search_input:
                            search_input = inp
                        break

        if form_score > best_score and search_input is not None:
            best_score = form_score
            best_form = form
            best_input_name = search_input.get("name") or search_input.get("id") or "q"

    if best_form is None or best_score < 2:
        return None

    # Collect hidden input fields (CSRF tokens, site IDs, etc.)
    hidden_fields = {}
    for inp in best_form.find_all("input", type="hidden"):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            hidden_fields[name] = value

    action = best_form.get("action", "")
    return action, best_input_name, hidden_fields


def _build_search_url(action: str, input_name: str, keyword: str,
                      hidden_fields: dict, base_url: str) -> str:
    """Constructs the absolute search URL with keyword and hidden fields."""
    # Resolve relative action against base URL
    absolute_action = urllib.parse.urljoin(base_url, action) if action else base_url

    params = {input_name: keyword}
    params.update(hidden_fields)

    # If action already contains query params, merge intelligently
    parsed_action = urllib.parse.urlparse(absolute_action)
    existing_params = dict(urllib.parse.parse_qsl(parsed_action.query))
    existing_params.update(params)

    new_query = urllib.parse.urlencode(existing_params)
    return urllib.parse.urlunparse(parsed_action._replace(query=new_query))


def _extract_result_urls(soup: BeautifulSoup, search_url: str,
                         origin_domain: str) -> Tuple[List[str], Optional[str]]:
    """
    Extracts content URLs from a search results page.

    Returns:
        - List of discovered content URLs (same-domain, non-navigation, non-asset).
        - Next page URL string if pagination is detected, else None.
    """
    discovered = []
    seen = set()
    next_page_url = None

    # Helper to parse page number from a URL
    def get_page_num(url: str) -> Optional[int]:
        parsed_url = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        for param in ('page', 'p', 'pg', 'paged', 'start', 'offset', 'from'):
            if param in query_params:
                val = query_params[param][0]
                if val.isdigit():
                    return int(val)
        return None

    current_page = get_page_num(search_url) or 1
    target_next_page = current_page + 1

    candidate_next_pages = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue

        abs_url = urllib.parse.urljoin(search_url, href)
        parsed = urllib.parse.urlparse(abs_url)

        # Only http/https
        if parsed.scheme not in ("http", "https"):
            continue

        # Must be same domain
        if not _is_same_domain(abs_url, origin_domain):
            continue

        # Skip static assets and pure anchors
        if _is_skippable_url(abs_url):
            continue

        # Detect pagination link — capture as next_page, don't add to results
        rel = a.get("rel", [])
        if isinstance(rel, list):
            rel = " ".join(rel)
        
        text = a.get_text().strip().lower()
        
        # Scenario A: Explicit rel="next"
        if "next" in rel.lower():
            next_page_url = abs_url
            continue

        # Scenario B: Text indicates next/forward/etc.
        is_text_next = any(x in text for x in ("next", ">", "»", "forward"))
        if is_text_next and _PAGINATION_RE.search(abs_url):
            next_page_url = abs_url
            continue

        # Scenario C: Keep track of pagination links for numerical page check
        if _PAGINATION_RE.search(abs_url):
            link_page = get_page_num(abs_url)
            if link_page == target_next_page:
                candidate_next_pages.append(abs_url)
            continue

        # Skip general navigation/pagination pages
        if _is_navigation_url(abs_url, search_url):
            continue

        # NEW: Skip search result pages and paginated search pages
        # This catches /page/2/?s=query (WordPress) and /search?q=query endpoints
        # that appear as links ON the search results page we just fetched.
        if _is_search_url(abs_url):
            continue

        # Deduplicate (strip fragment)
        clean_url = urllib.parse.urlunparse(parsed._replace(fragment=""))
        if clean_url not in seen:
            seen.add(clean_url)
            discovered.append(clean_url)

    # Also check <link rel="next"> in <head> for paginated search results
    if not next_page_url:
        link_next = soup.find("link", rel=lambda r: r and "next" in r)
        if link_next and link_next.get("href"):
            next_page_url = urllib.parse.urljoin(search_url, link_next.get("href"))

    # Fallback to numerical target next page candidate
    if not next_page_url and candidate_next_pages:
        next_page_url = candidate_next_pages[0]

    return discovered, next_page_url


class SiteSearchDetector:
    """
    Detects site-native search on a URL, executes the search with a keyword,
    and returns all discovered result URLs.

    Usage:
        detector = SiteSearchDetector(crawler_instance)
        urls = detector.discover(url="https://example.com/news", keyword="climate")
        if urls is None:
            # No site search found — fall through to existing crawl logic
            pass
        else:
            # Use urls as candidate_urls
            pass
    """

    def __init__(self, crawler):
        """
        Args:
            crawler: An existing backend.crawler.Crawler instance.
                     Reuses its fetch_page() and session — never creates its own.
        """
        self._crawler = crawler

    def _fetch_html(self, url: str, engine: str = "fast",
                    ignore_robots: bool = False) -> Optional[str]:
        """Fetches HTML using the shared crawler. Returns None on any failure."""
        try:
            return self._crawler.fetch_page(url, engine=engine,
                                            ignore_robots=ignore_robots)
        except Exception:
            return None

    def _detect_on_page(self, url: str, engine: str,
                        ignore_robots: bool) -> Optional[Tuple[str, str, dict, str]]:
        """
        Tries to detect a search form on the given URL.

        Returns: (action, input_name, hidden_fields, base_url) or None.
        """
        html = self._fetch_html(url, engine, ignore_robots)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        result = _detect_search_form(soup)
        if result:
            action, input_name, hidden_fields = result
            return action, input_name, hidden_fields, url

        return None

    def discover(
        self,
        url: str,
        keyword: str,
        engine: str = "fast",
        ignore_robots: bool = False
    ) -> Optional[List[str]]:
        """
        Main entry point. Attempts site search detection and discovery.

        Detection order:
          1. Try the submitted URL itself.
          2. If not found, try the homepage (scheme://netloc/).

        Returns:
          - List[str]: discovered URLs if site search was found and used.
          - None: if no site search detected OR if any error occurs during discovery.
            Caller must treat None as "fall through to existing crawl logic".
        """
        try:
            origin = _get_origin(url)
            origin_domain = _get_domain(url)
            homepage = origin + "/"

            # Step 1: Try to detect on the submitted URL
            detection = self._detect_on_page(url, engine, ignore_robots)

            # Step 2: If not found, try the homepage (only if different from submitted URL)
            if detection is None and url.rstrip("/") != homepage.rstrip("/"):
                detection = self._detect_on_page(homepage, engine, ignore_robots)

            if detection is None:
                return None  # No site search found — caller falls through

            action, input_name, hidden_fields, base_url = detection

            # Step 3: Build the search URL
            search_url = _build_search_url(
                action, input_name, keyword, hidden_fields, base_url
            )

            # Step 4: Fetch first search results page
            results_html = self._fetch_html(search_url, engine, ignore_robots)
            if not results_html:
                return None  # Search page fetch failed — fall through

            # Step 5: Extract URLs from results, follow pagination
            all_discovered: List[str] = []
            seen_pages: set = {search_url}
            current_url = search_url
            current_html = results_html
            pages_followed = 0

            while True:
                soup = BeautifulSoup(current_html, "html.parser")
                page_urls, next_page = _extract_result_urls(
                    soup, current_url, origin_domain
                )

                for u in page_urls:
                    if u not in set(all_discovered):
                        all_discovered.append(u)

                # Stop if we have enough URLs
                if len(all_discovered) >= MAX_DISCOVERY_URLS:
                    break

                # Stop if no next page or we've followed enough pages
                if (not next_page or next_page in seen_pages
                        or pages_followed >= MAX_PAGINATION_PAGES):
                    break

                # Follow pagination
                seen_pages.add(next_page)
                next_html = self._fetch_html(next_page, engine, ignore_robots)
                if not next_html:
                    break

                current_url = next_page
                current_html = next_html
                pages_followed += 1

            # Return None if we found zero URLs (likely a detection false-positive)
            # so the caller falls through to the existing link-expansion logic
            if not all_discovered:
                return None

            return all_discovered[:MAX_DISCOVERY_URLS]

        except Exception:
            # Never raise to caller — always return None on any unhandled error
            return None
