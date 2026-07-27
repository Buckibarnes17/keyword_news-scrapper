# ## Changes (Trafilatura Integration — KeywordScout v2.0 Upgrade)
# - Added sitemap and feed source type discovery options to process_search_query.
# - Implemented in-memory SimHash fuzzy near-duplicate checks in the crawled items processor loop.
# - Added site-native search detection gate in process_search_query() direct branch.
#   SiteSearchDetector runs before link-expansion; returns None → existing logic unchanged.
#   Returns List[str] → candidate_urls populated from site search results, link-expansion skipped.
# ## Changes
# - Integrated url_classifier.filter_candidate_urls() after deduplication in
#   process_search_query(). Catches search pages from all candidate URL sources
#   (site search discovery, link-expansion, and site-restricted search_web).
# - Imported re, Tuple, Any, List, Dict, Set, and timezone from typing/datetime.
# - Renamed stop event to _queue_stop_event to avoid collision with scheduler.
# - Replaced datetime.utcnow() with datetime.now(timezone.utc).
# - Implemented database-atomic counter increments for crawled and matched counters using sqlalchemy.update.
# - Filtered candidate URLs with domains_filter inclusions and exclusions.
# - Filtered crawl results post-parse by languages_filter, date_range_start, and date_range_end.
# - Added unhandled worker crash safety net in queue_worker_loop to mark crashed queries as failed.

import time
import re
import threading
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Set, Tuple, Any
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
from sqlalchemy.orm import Session
from sqlalchemy import update, select, func
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

from backend.database import SessionLocal
from backend.models import SearchQuery, CrawledURL, KeywordProgress
from backend.search_engine import search_web
from backend.crawler import Crawler
from bs4 import BeautifulSoup
from backend.site_search_detector import SiteSearchDetector
from backend.url_classifier import filter_candidate_urls

# Thread-safe shared state variables
_shared_aborted_jobs = {}
_shared_aborted_lock = threading.Lock()
_shared_domain_last_crawl = {}
_shared_domain_lock = threading.Lock()
_shared_process_stop_event = threading.Event()

# Fallback structures for non-multiprocessed local tasks
DOMAIN_LAST_CRAWL: Dict[str, float] = {}
domain_lock = threading.Lock()

def request_job_stop(search_id: int):
    with _shared_aborted_lock:
        _shared_aborted_jobs[search_id] = True

def is_job_stopped(search_id: int) -> bool:
    with _shared_aborted_lock:
        return _shared_aborted_jobs.get(search_id, False)

# Worker thread variables
_worker_thread = None
_queue_stop_event = threading.Event()

def parse_sitemap_urls(xml_content: str) -> List[str]:
    """Parses URLs from an XML sitemap."""
    urls = []
    try:
        # Remove namespace prefixes for easier parsing if they exist
        xml_content_clean = re.sub(r'\sxmlns="[^"]+"', '', xml_content, count=1)
        root = ET.fromstring(xml_content_clean.encode('utf-8'))
        for url_node in root.findall('.//url/loc'):
            if url_node.text:
                urls.append(url_node.text.strip())
    except Exception as e:
        print(f"Error parsing XML sitemap: {e}")
    return urls

def fetch_direct_urls(url_list_str: str, session: Session, query = None) -> List[str]:
    """
    Parses and sanitizes input URLs.
    If a URL is an XML sitemap, it fetches and extracts all URLs inside it.
    """
    if not url_list_str:
        return []
        
    raw_urls = [line.strip() for line in url_list_str.split("\n") if line.strip()]
    plain_urls = []
    sitemap_urls_to_fetch = []

    for url in raw_urls:
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        parsed = urlparse(url)
        path = parsed.path.lower()
        if path.endswith(".xml") or "sitemap" in path:
            sitemap_urls_to_fetch.append(url)
        else:
            plain_urls.append(url)

    # Fetch all sitemaps in parallel
    def _fetch_sitemap(sm_url):
        local_crawler = Crawler(proxy_url=getattr(query, 'proxy_url', None))
        try:
            xml_text = local_crawler.fetch_page(sm_url, engine="fast", ignore_robots=True)
            return parse_sitemap_urls(xml_text)
        except Exception as e:
            print(f"Failed to fetch sitemap {sm_url}: {e}")
            return [sm_url]  # Fallback to direct URL
        finally:
            local_crawler.close()

    sm_results = []
    if sitemap_urls_to_fetch:
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        sm_pool = ThreadPoolExecutor(max_workers=min(5, len(sitemap_urls_to_fetch)))
        sm_futures = [sm_pool.submit(_fetch_sitemap, u) for u in sitemap_urls_to_fetch]
        pending = list(sm_futures)
        start_time = time.time()
        try:
            while pending:
                if is_job_stopped(query.id):
                    for f in pending:
                        f.cancel()
                    break
                if time.time() - start_time > 45.0:
                    print("[Watchdog Warning] Parallel sitemap fetching in fetch_direct_urls timed out after 45s.")
                    for f in pending:
                        f.cancel()
                    break
                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        sm_results.extend(fut.result())
                    except Exception:
                        pass
        finally:
            wait_on_shutdown = not is_job_stopped(query.id)
            sm_pool.shutdown(wait=wait_on_shutdown)

    final_urls = plain_urls + sm_results
    return list(dict.fromkeys(final_urls))  # Deduplicate

def crawl_url_task(
    url_id: int,
    search_id: int,
    keyword: str,
    match_type: str,
    case_sensitive: bool,
    exact_match: bool,
    engine: str,
    ignore_robots: bool = False,
    proxy_url: str = None,                  # NEW — add here
    languages_filter: List[str] = None,
    date_range_start: datetime = None,
    date_range_end: datetime = None,
    domain_rate_limit: float = None,  # None → read from env, fallback 1.0s
    shared_aborted_jobs_dict = None,
    shared_aborted_lock = None,
    shared_domain_last_crawl_dict = None,
    shared_domain_lock = None,
    shared_process_stop_event = None
) -> Tuple[int, Dict[str, Any]]:
    """
    Runs within a worker process. Fetches a URL with rate limiting,
    analyzes keyword content, and returns results.
    """
    def check_stopped() -> bool:
        if shared_process_stop_event is not None and shared_process_stop_event.is_set():
            return True
        if shared_aborted_jobs_dict is not None and shared_aborted_lock is not None:
            with shared_aborted_lock:
                return shared_aborted_jobs_dict.get(search_id, False)
        return is_job_stopped(search_id)

    db = None
    result = {"status": "failed", "error_message": None}
    try:
        db = SessionLocal()
        crawled_url = db.scalars(select(CrawledURL).where(CrawledURL.id == url_id)).first()
        if not crawled_url:
            db.close()
            db = None
            return url_id, {"status": "failed", "error_message": "URL record not found in database."}

        if check_stopped():
            db.close()
            db = None
            return url_id, {"status": "skipped", "error_message": "Job aborted by user."}

        url = crawled_url.url
        domain = crawled_url.domain

        # Close DB session early and safely now that we have read the attributes
        db.close()
        db = None

        # Resolve effective rate limit (env override → passed arg → hardcoded default)
        import os as _os
        if domain_rate_limit is None:
            try:
                domain_rate_limit = float(_os.environ.get("KS_DOMAIN_RATE_LIMIT", "1.0"))
            except (TypeError, ValueError):
                domain_rate_limit = 1.0

        # 1. Enforce Domain Rate Limiting
        while True:
            if check_stopped():
                return url_id, {"status": "skipped", "error_message": "Job aborted by user."}
                
            if shared_domain_last_crawl_dict is not None and shared_domain_lock is not None:
                with shared_domain_lock:
                    now = time.time()
                    last_crawl = shared_domain_last_crawl_dict.get(domain, 0.0)
                    elapsed = now - last_crawl
                    remaining = domain_rate_limit - elapsed
                    if remaining <= 0:
                        shared_domain_last_crawl_dict[domain] = now
                        break
            else:
                with domain_lock:
                    now = time.time()
                    last_crawl = DOMAIN_LAST_CRAWL.get(domain, 0.0)
                    elapsed = now - last_crawl
                    remaining = domain_rate_limit - elapsed
                    if remaining <= 0:
                        DOMAIN_LAST_CRAWL[domain] = now
                        break
                        
            # Sleep for the exact remaining duration to avoid active spinning
            time.sleep(max(0.01, remaining))

        crawler = Crawler(proxy_url=proxy_url)
        
        # 2. Fetch page with retries (up to 2 retries, exponential backoff)
        max_retries = 2
        retry_delay = 1.0
        html_content = ""
        
        for attempt in range(max_retries):
            if check_stopped():
                result = {"status": "skipped", "error_message": "Job aborted by user."}
                break
            try:
                html_content = crawler.fetch_page(url, engine=engine, ignore_robots=ignore_robots)
                result["error_message"] = None
                break  # Success
            except PermissionError as pe:
                result = {"status": "skipped", "error_message": "Forbidden by robots.txt"}
                break
            except Exception as e:
                result["error_message"] = str(e)
                
                # Check if it is a non-retryable HTTP status code
                is_retryable = True
                # Check if exception has response attribute (indicating an HTTPError)
                response = getattr(e, "response", None)
                if response is not None:
                    status_code = getattr(response, "status_code", None)
                    if status_code in (404, 410, 401, 403):
                        is_retryable = False
                
                if is_retryable and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Double delay (4s, 8s)
                else:
                    result["status"] = "failed"
                    break
                    
        crawler.close()
        
        # 3. Analyze page content if fetch was successful
        if html_content:
            try:
                effective_keyword = "" if keyword == "__config__" else keyword
                analysis = crawler.analyze_page(
                    html_content=html_content,
                    url=url,
                    keyword=effective_keyword,
                    match_type=match_type,
                    case_sensitive=case_sensitive,
                    exact_match=exact_match
                )
                result.update(analysis)
                # If matched, status is "matched", else "skipped"
                result["status"] = "matched" if analysis["matched"] else "skipped"
                
                # Apply language filter
                if languages_filter and analysis.get("language"):
                    if analysis["language"] not in languages_filter:
                        result["status"] = "skipped"
                        result["error_message"] = f"Language '{analysis['language']}' not in filter."
                        
                # Apply date range filters
                pub_date = analysis.get("discovered_at")
                if pub_date and isinstance(pub_date, datetime):
                    # If pub_date is timezone-naive, make it timezone-aware to match date_range_start/end
                    if pub_date.tzinfo is None:
                        pub_date = pub_date.replace(tzinfo=timezone.utc)
                    
                    # If no specific start date is implied, default to 3 months ago (90 days)
                    start_date = date_range_start
                    if start_date is None:
                        start_date = datetime.now(timezone.utc) - timedelta(days=90)
                    
                    if start_date.tzinfo is None:
                        start_date = start_date.replace(tzinfo=timezone.utc)
                    if pub_date < start_date:
                        result["status"] = "skipped"
                        result["error_message"] = "Page date before date_range_start."
                    if date_range_end:
                        end_date = date_range_end
                        if end_date.tzinfo is None:
                            end_date = end_date.replace(tzinfo=timezone.utc)
                        if pub_date > end_date:
                            result["status"] = "skipped"
                            result["error_message"] = "Page date after date_range_end."
            except Exception as e:
                result["status"] = "failed"
                result["error_message"] = f"Parsing Error: {str(e)}"
                
    finally:
        if db is not None:
            db.close()
    return url_id, result

def run_direct_discovery(unique_raw_urls: List[str], query) -> Tuple[Dict[str, List[str]], Set[str]]:
    """
    Runs keyword-independent URL discovery (sitemap, feed, or link expansion)
    once per unique URL.
    Returns:
      - A dict mapping d_url -> List[discovered_urls]
      - A set of bare domains of the direct URLs
    """
    discovered_candidates = {}
    searched_domains = set()

    def _discover_single(d_url: str) -> Tuple[str, List[str], Optional[str]]:
        if is_job_stopped(query.id):
            return d_url, [], None

        if not (d_url.startswith("http://") or d_url.startswith("https://")):
            return d_url, [], None

        parsed = urlparse(d_url)
        path = parsed.path.lower()

        # Get bare domain
        od = parsed.netloc.lower()
        domain = od[4:] if od.startswith("www.") else od

        # Sitemap check
        if path.endswith(".xml") or "sitemap" in path:
            try:
                from backend.sitemap_discovery import discover_from_sitemap
                if is_job_stopped(query.id):
                    return d_url, [], domain
                urls = discover_from_sitemap(d_url, max_urls=500)
                print(f"[SitemapDiscovery] Found {len(urls)} URLs for sitemap: {d_url}")
                return d_url, urls, domain
            except Exception as e:
                print(f"[SitemapDiscovery] Failed for {d_url}: {e}")
                return d_url, [d_url], domain

        # Feed check
        elif "rss" in path or "feed" in path or "atom" in path or path.endswith("/feed") or path.endswith("/feed/"):
            try:
                from backend.sitemap_discovery import discover_from_feeds
                if is_job_stopped(query.id):
                    return d_url, [], domain
                urls = discover_from_feeds(d_url, max_urls=200)
                print(f"[FeedDiscovery] Found {len(urls)} URLs for feed: {d_url}")
                return d_url, urls, domain
            except Exception as e:
                print(f"[FeedDiscovery] Failed for {d_url}: {e}")
                return d_url, [d_url], domain

        # Regular URL -> link expansion
        else:
            candidates = [d_url]
            local_crawler = Crawler(proxy_url=getattr(query, 'proxy_url', None))
            try:
                if is_job_stopped(query.id):
                    return d_url, [d_url], domain
                html_text = local_crawler.fetch_page(d_url, engine="fast", ignore_robots=getattr(query, 'ignore_robots', False))
                if is_job_stopped(query.id):
                    return d_url, [d_url], domain
                soup = BeautifulSoup(html_text, "html.parser")
                count = 0
                for a in soup.find_all("a"):
                    if is_job_stopped(query.id):
                        break
                    if count >= 100:
                        break
                    href = a.get("href")
                    if href:
                        abs_url = urljoin(d_url, href.strip())
                        parsed_abs = urlparse(abs_url)
                        if parsed_abs.scheme in ("http", "https"):
                            abs_domain = parsed_abs.netloc.lower()
                            if abs_domain.startswith("www."):
                                abs_domain = abs_domain[4:]
                            if abs_domain == domain:
                                abs_path = parsed_abs.path.lower()
                                if not any(abs_path.endswith(ext) for ext in [".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".css", ".js", ".xml"]):
                                    candidates.append(abs_url)
                                    count += 1
                print(f"[LinkExpansion] Discovered {len(candidates)} URLs for: {d_url}")
            except Exception as ex:
                print(f"[WARNING] Failed to expand links for {d_url}: {ex}")
            finally:
                local_crawler.close()
            return d_url, candidates, domain

    # Concurrently run discovery for each direct URL (I/O bound)
    if unique_raw_urls:
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        exp_pool = ThreadPoolExecutor(max_workers=min(16, len(unique_raw_urls)))
        futures = [exp_pool.submit(_discover_single, u) for u in unique_raw_urls]
        from concurrent.futures import wait, FIRST_COMPLETED
        pending = list(futures)
        start_time = time.time()
        try:
            while pending:
                if is_job_stopped(query.id):
                    for f in pending:
                        f.cancel()
                    break

                # Implement 45s watchdog on the entire discovery phase
                if time.time() - start_time > 45.0:
                    print("[Watchdog Warning] Direct URL discovery timed out after 45s. Cancelling pending discovery tasks.")
                    for f in pending:
                        f.cancel()
                    break

                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        d_url, candidates, domain = fut.result()
                        discovered_candidates[d_url] = list(dict.fromkeys(candidates))
                        if domain:
                            searched_domains.add(domain)
                    except Exception as ex:
                        print(f"[WARNING] Direct URL discovery future failed: {ex}")
        finally:
            wait_on_shutdown = not is_job_stopped(query.id)
            exp_pool.shutdown(wait=wait_on_shutdown)

    return discovered_candidates, searched_domains

def _process_single_keyword(
    search_id: int,
    kw: str,
    query,
    languages_filter,
    seen_content_hashes: set,
    seen_simhashes: list,
    seen_lock: threading.Lock,
    total_keyword_count: int,
    pre_discovered_candidates: Dict[str, List[str]] = None,
    pre_discovered_domains: Set[str] = None,
) -> None:
    """
    Processes a single keyword within a search job: discovers candidate URLs,
    crawls them, deduplicates results, and writes to DB.
    Designed to run concurrently with other keywords via ThreadPoolExecutor.
    """
    db = SessionLocal()
    try:
        if _queue_stop_event.is_set() or is_job_stopped(search_id):
            return

        kp_record = db.scalars(select(KeywordProgress).where(
            KeywordProgress.search_query_id == search_id,
            KeywordProgress.keyword == kw
        )).first()

        if kp_record and kp_record.status == "completed":
            print(f"Skipping completed keyword '{kw}' for search {search_id}")
            return

        if kp_record:
            kp_record.status = "processing"
            kp_record.started_at = datetime.now(timezone.utc)
            kp_record.completed_at = None
            db.commit()

        # Parse domains_filter
        domains_include = []
        domains_exclude = []
        if query.domains_filter:
            try:
                df = json.loads(query.domains_filter)
                domains_include = [d.lower() for d in df.get("include", []) if d.strip()]
                domains_exclude = [d.lower() for d in df.get("exclude", []) if d.strip()]
            except Exception as e:
                print(f"Error parsing domains_filter: {e}")

        # ── Step 1: Gather candidate URLs ──
        candidate_urls = []
        if query.source_type == "direct":
            searched_domains = set()
            if pre_discovered_candidates is not None:
                raw_direct_urls = list(pre_discovered_candidates.keys())
                searched_domains = set(pre_discovered_domains or [])
                
                # Check site-native search detection for each raw direct URL if keyword is active
                site_search_urls = []
                site_search_domains_to_skip_expansion = set()
                
                if kw.strip() and kw != "__config__":
                    def _check_site_search(d_url):
                        local_crawler = Crawler(proxy_url=query.proxy_url)
                        try:
                            _detector = SiteSearchDetector(local_crawler)
                            _discovered = _detector.discover(
                                url=d_url, keyword=kw,
                                engine=query.engine, ignore_robots=query.ignore_robots
                            )
                            if _discovered:
                                print(f"[SiteSearch] {len(_discovered)} URLs via site-native search: {d_url}")
                                return d_url, _discovered
                        except Exception as ex:
                            print(f"[WARNING] Site-native search check failed for {d_url}: {ex}")
                        finally:
                            local_crawler.close()
                        return d_url, None

                    # Concurrently check site search
                    ss_pool = ThreadPoolExecutor(max_workers=min(8, len(raw_direct_urls) or 1))
                    ss_futures = [ss_pool.submit(_check_site_search, u) for u in raw_direct_urls]
                    from concurrent.futures import wait, FIRST_COMPLETED
                    pending = list(ss_futures)
                    try:
                        while pending:
                            if is_job_stopped(search_id):
                                for f in pending:
                                    f.cancel()
                                break
                            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                            for fut in done:
                                try:
                                    d_url, discovered_urls = fut.result()
                                    if discovered_urls:
                                        site_search_urls.extend(discovered_urls)
                                        site_search_domains_to_skip_expansion.add(d_url)
                                except Exception as ex:
                                    print(f"[WARNING] Site search future failed: {ex}")
                    finally:
                        wait_on_shutdown = not is_job_stopped(search_id)
                        ss_pool.shutdown(wait=wait_on_shutdown)

                # Build candidate_urls list:
                for d_url in raw_direct_urls:
                    if d_url in site_search_domains_to_skip_expansion:
                        pass
                    else:
                        candidate_urls.extend(pre_discovered_candidates.get(d_url, [d_url]))

                # Add the site_search_urls
                candidate_urls.extend(site_search_urls)
                
            else:
                # Fallback implementation
                raw_direct_urls = [line.strip() for line in (query.direct_urls or "").split("\n") if line.strip()]
                unique_raw_urls = list(dict.fromkeys(raw_direct_urls))
                
                temp_candidates, searched_domains = run_direct_discovery(unique_raw_urls, query)
                
                site_search_urls = []
                site_search_domains_to_skip_expansion = set()
                if kw.strip() and kw != "__config__":
                    def _check_site_search_fallback(d_url):
                        local_crawler = Crawler(proxy_url=query.proxy_url)
                        try:
                            _detector = SiteSearchDetector(local_crawler)
                            _discovered = _detector.discover(
                                url=d_url, keyword=kw,
                                engine=query.engine, ignore_robots=query.ignore_robots
                            )
                            if _discovered:
                                return d_url, _discovered
                        except Exception:
                            pass
                        finally:
                            local_crawler.close()
                        return d_url, None

                    ss_pool = ThreadPoolExecutor(max_workers=min(8, len(unique_raw_urls) or 1))
                    ss_futures = [ss_pool.submit(_check_site_search_fallback, u) for u in unique_raw_urls]
                    from concurrent.futures import wait, FIRST_COMPLETED
                    pending = list(ss_futures)
                    try:
                        while pending:
                            if is_job_stopped(search_id):
                                for f in pending:
                                    f.cancel()
                                break
                            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                            for fut in done:
                                try:
                                    d_url, discovered_urls = fut.result()
                                    if discovered_urls:
                                        site_search_urls.extend(discovered_urls)
                                        site_search_domains_to_skip_expansion.add(d_url)
                                except Exception:
                                    pass
                    finally:
                        wait_on_shutdown = not is_job_stopped(search_id)
                        ss_pool.shutdown(wait=wait_on_shutdown)

                for d_url in unique_raw_urls:
                    if d_url in site_search_domains_to_skip_expansion:
                        pass
                    else:
                        candidate_urls.extend(temp_candidates.get(d_url, [d_url]))
                candidate_urls.extend(site_search_urls)

            # Parallel site-restricted search for all discovered domains
            if kw.strip() and kw != "__config__" and searched_domains:
                def _site_restricted_search(domain):
                    try:
                        _tor_proxies = None
                        try:
                            from backend.tor_router import TOR_REQUESTS_PROXIES, is_tor_proxy_url
                            if is_tor_proxy_url(query.proxy_url or ""):
                                _tor_proxies = TOR_REQUESTS_PROXIES
                        except ImportError:
                            pass
                        results = search_web(f"{kw} site:{domain}", max_results=50, tor_proxies=_tor_proxies)
                        print(f"[INFO] Site-restricted search '{domain}' -> {len(results)} URLs")
                        return results
                    except Exception as sex:
                        print(f"[WARNING] Site-restricted search failed for '{domain}': {sex}")
                        return []

                sr_pool = ThreadPoolExecutor(max_workers=min(10, len(searched_domains)))
                sr_futures = [sr_pool.submit(_site_restricted_search, d) for d in searched_domains]
                from concurrent.futures import wait, FIRST_COMPLETED
                pending = list(sr_futures)
                try:
                    while pending:
                        if is_job_stopped(search_id):
                            for f in pending:
                                f.cancel()
                            break
                        done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                        for fut in done:
                            try:
                                candidate_urls.extend(fut.result())
                            except Exception:
                                pass
                finally:
                    wait_on_shutdown = not is_job_stopped(search_id)
                    sr_pool.shutdown(wait=wait_on_shutdown)

            candidate_urls = list(dict.fromkeys(candidate_urls))
            candidate_urls = filter_candidate_urls(candidate_urls)

        elif query.source_type == "sitemap":
            base_url = (query.direct_urls or "").strip().splitlines()[0].strip()
            if pre_discovered_candidates is not None and base_url in pre_discovered_candidates:
                candidate_urls = pre_discovered_candidates[base_url]
            else:
                from backend.sitemap_discovery import discover_from_sitemap
                candidate_urls = list(dict.fromkeys(discover_from_sitemap(base_url, max_urls=500)))
        elif query.source_type == "feed":
            base_url = (query.direct_urls or "").strip().splitlines()[0].strip()
            if pre_discovered_candidates is not None and base_url in pre_discovered_candidates:
                candidate_urls = pre_discovered_candidates[base_url]
            else:
                from backend.sitemap_discovery import discover_from_feeds
                candidate_urls = list(dict.fromkeys(discover_from_feeds(base_url, max_urls=200)))
        else:
            if kw == "__config__":
                candidate_urls = []
            else:
                search_query = kw
                if domains_include:
                    if len(domains_include) == 1:
                        search_query = f"{kw} site:{domains_include[0]}"
                    else:
                        search_query = f"{kw} (" + " OR ".join(f"site:{d}" for d in domains_include) + ")"
                # Resolve tor_proxies: inject when job's proxy_url is the Tor SOCKS5 address
                _tor_proxies = None
                try:
                    from backend.tor_router import TOR_REQUESTS_PROXIES, is_tor_proxy_url
                    if is_tor_proxy_url(query.proxy_url or ""):
                        _tor_proxies = TOR_REQUESTS_PROXIES
                except ImportError:
                    pass
                candidate_urls = search_web(search_query, max_results=100, tor_proxies=_tor_proxies)

        def get_domain(url):
            d = urlparse(url).netloc.lower()
            return d[4:] if d.startswith("www.") else d

        if domains_include:
            candidate_urls = [u for u in candidate_urls if any(get_domain(u).endswith(d) for d in domains_include)]
        if domains_exclude:
            candidate_urls = [u for u in candidate_urls if not any(get_domain(u).endswith(d) for d in domains_exclude)]

        # Candidate cap
        import os as _os3
        _max_candidates = int(_os3.environ.get("KS_MAX_CANDIDATE_URLS", "500"))
        if len(candidate_urls) > _max_candidates:
            print(f"[Cap] Trimming {len(candidate_urls)} to {_max_candidates} for '{kw}'")
            candidate_urls = candidate_urls[:_max_candidates]

        if not candidate_urls:
            if kp_record:
                kp_record.status = "completed"
                kp_record.articles_found = 0
                kp_record.completed_at = datetime.now(timezone.utc)
                db.commit()
            return

        # ── Step 2: Initialize DB records — bulk approach ───────────────────────────
        # Single query to find already-existing URLs for this search_id
        existing_records = {
            r.url: r for r in db.scalars(select(CrawledURL).where(
                CrawledURL.search_id == search_id
            )).all()
        }

        new_records = []
        db_urls = []
        for url in candidate_urls:
            if url in existing_records:
                existing = existing_records[url]
                if existing.status in ("pending", "failed"):
                     db_urls.append(existing)
            else:
                parsed = urlparse(url)
                domain_val = parsed.netloc.lower()
                if domain_val.startswith("www."):
                    domain_val = domain_val[4:]
                new_rec = CrawledURL(
                    search_id=search_id,
                    url=url,
                    domain=domain_val,
                    status="pending"
                )
                new_records.append(new_rec)
                db_urls.append(new_rec)

        if new_records:
            db.add_all(new_records)
            db.commit()
            # Reload to get DB-assigned IDs
            for r in new_records:
                db.refresh(r)

        unique_urls_count = db.execute(select(func.count(CrawledURL.id)).where(CrawledURL.search_id == search_id)).scalar() or 0
        db.execute(
            update(SearchQuery)
            .where(SearchQuery.id == search_id)
            .values(total_urls_found=unique_urls_count)
        )
        db.commit()

        # ── Step 3: Crawl pool ──────────────────────────────────────────────────────
        import os as _os_w
        if query.engine == "fast":
            base_max = int(_os_w.environ.get("KS_FAST_WORKERS", "25"))
        elif query.engine == "lightpanda":
            base_max = int(_os_w.environ.get("KS_LIGHTPANDA_WORKERS", "10"))
        else:
            base_max = int(_os_w.environ.get("KS_SELENIUM_WORKERS", "4"))

        # Adaptive formula to prevent thread explosion (total threads capped ~150)
        url_workers = max(8, min(30, 150 // max(1, total_keyword_count)))
        max_workers = min(base_max, url_workers)

        # Pre-warm Selenium driver before the pool starts (avoids 25-thread lock contention
        # on first driver init; driver is cached on the Crawler instance after first call)
        if query.engine == "dynamic":
            try:
                _warmup_crawler = Crawler(proxy_url=query.proxy_url)
                _warmup_crawler._get_selenium_driver()
                _warmup_crawler.close()
            except Exception as _warmup_err:
                print(f"[Selenium Warmup] Pre-warm failed (non-fatal): {_warmup_err}")

        futures = {}
        executor = ThreadPoolExecutor(max_workers=max_workers)
        has_abandoned = False
        try:
            for db_url in db_urls:
                future = executor.submit(
                    crawl_url_task,
                    url_id=db_url.id,
                    search_id=search_id,
                    keyword=kw,
                    match_type=query.match_type,
                    case_sensitive=query.case_sensitive,
                    exact_match=query.exact_match,
                    engine=query.engine,
                    ignore_robots=query.ignore_robots,
                    proxy_url=query.proxy_url,
                    languages_filter=languages_filter,
                    date_range_start=query.date_range_start,
                    date_range_end=query.date_range_end,
                    domain_rate_limit=None,
                    shared_aborted_jobs_dict=_shared_aborted_jobs,
                    shared_aborted_lock=_shared_aborted_lock,
                    shared_domain_last_crawl_dict=_shared_domain_last_crawl,
                    shared_domain_lock=_shared_domain_lock,
                    shared_process_stop_event=_shared_process_stop_event
                )
                futures[future] = {"url_id": db_url.id, "start_time": time.time()}

            from concurrent.futures import wait, FIRST_COMPLETED

            pending = list(futures.keys())
            while pending:
                if _queue_stop_event.is_set() or is_job_stopped(search_id):
                    # Cancel outstanding tasks on abort/stop
                    for fut in pending:
                        fut.cancel()
                    db.commit()
                    break

                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)

                for future in done:
                    info = futures[future]
                    url_id = info["url_id"]
                    try:
                        url_id, result = future.result()
                    except Exception as fut_err:
                        print(f"[ERROR] Crawl task future failed: {fut_err}")
                        result = {"status": "failed", "error_message": f"Task error: {str(fut_err)}"}

                    # Dedup check with shared cross-keyword lock
                    if result.get("content_hash") and result["status"] in ("matched", "skipped"):
                        h = result["content_hash"]
                        is_dup = False
                        with seen_lock:
                            if h in seen_content_hashes:
                                is_dup = True
                            else:
                                if result.get("simhash"):
                                    from backend.simhash_dedup import is_near_duplicate
                                    for existing_sh in seen_simhashes:
                                        if is_near_duplicate(result["simhash"], existing_sh):
                                            is_dup = True
                                            break
                                if not is_dup:
                                    seen_content_hashes.add(h)
                                    if result.get("simhash"):
                                        seen_simhashes.append(result["simhash"])
                        if is_dup:
                            result["status"] = "skipped"
                            result["is_duplicate"] = True
                            result["error_message"] = "Duplicate page content detected."

                    db_item = db.scalars(select(CrawledURL).where(CrawledURL.id == url_id)).first()
                    if db_item:
                        for key, val in result.items():
                            if hasattr(db_item, key):
                                setattr(db_item, key, val)
                        db.flush()

                        if db_item.status != "pending":
                            inc_crawled = 1
                        else:
                            inc_crawled = 0
                        
                        if db_item.status == "matched":
                            inc_matched = 1
                        else:
                            inc_matched = 0

                        # Write progress incrementally to DB per-page
                        if inc_crawled > 0 or inc_matched > 0:
                            db.execute(
                                update(SearchQuery)
                                .where(SearchQuery.id == search_id)
                                .values(
                                    total_urls_crawled=SearchQuery.total_urls_crawled + inc_crawled,
                                    total_urls_matched=SearchQuery.total_urls_matched + inc_matched
                                )
                            )
                            if inc_matched > 0:
                                db.execute(
                                    update(KeywordProgress)
                                    .where(KeywordProgress.search_query_id == search_id)
                                    .where(KeywordProgress.keyword == kw)
                                    .values(articles_found=KeywordProgress.articles_found + inc_matched)
                                )
                            db.commit()

                # Watchdog check for tasks running > 45s
                now = time.time()
                still_pending = []
                for future in pending:
                    info = futures[future]
                    elapsed = now - info["start_time"]
                    if elapsed > 45.0:
                        has_abandoned = True
                        url_id = info["url_id"]
                        future.cancel()  # Try to cancel
                        print(f"[Watchdog] Crawl task for URL ID {url_id} timed out (>45s) and was abandoned.")

                        db_item = db.scalars(select(CrawledURL).where(CrawledURL.id == url_id)).first()
                        if db_item and db_item.status == "pending":
                            db_item.status = "failed"
                            db_item.error_message = "Task exceeded maximum duration of 45 seconds."
                            db.flush()

                            db.execute(
                                update(SearchQuery)
                                .where(SearchQuery.id == search_id)
                                .values(total_urls_crawled=SearchQuery.total_urls_crawled + 1)
                            )
                            db.commit()
                    else:
                        still_pending.append(future)
                pending = still_pending
        finally:
            wait_on_shutdown = not (is_job_stopped(search_id) or _queue_stop_event.is_set() or has_abandoned)
            executor.shutdown(wait=wait_on_shutdown)

        # Mark keyword progress
        if is_job_stopped(search_id):
            if kp_record:
                kp_record.status = "failed"
                kp_record.completed_at = datetime.now(timezone.utc)
                db.commit()
        else:
            kw_matched = db.execute(select(func.count(CrawledURL.id)).where(
                CrawledURL.search_id == search_id,
                CrawledURL.id.in_([u.id for u in db_urls]),
                CrawledURL.status == "matched"
            )).scalar() or 0
            if kp_record:
                kp_record.status = "completed"
                kp_record.articles_found = kw_matched
                kp_record.completed_at = datetime.now(timezone.utc)
                db.commit()

    except Exception as e:
        print(f"[ERROR] _process_single_keyword failed for '{kw}': {e}")
        if kp_record:
            try:
                kp_record.status = "failed"
                kp_record.completed_at = datetime.now(timezone.utc)
                db.commit()
            except Exception:
                pass
    finally:
        db.close()

def process_search_query(search_id: int):
    """
    Pulls candidate URLs (from search engine or direct inputs),
    spawns a thread pool to crawl them, detects duplicates, updates progress,
    and handles final query status.
    """
    db = SessionLocal()
    query = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
    if not query:
        db.close()
        return

    # Update state to processing
    process_search_query._batch_counter = 0
    query.status = "processing"
    query.updated_at = datetime.now(timezone.utc)
    db.commit()

    try:
        # Parse list of search keywords
        keywords_list = []
        try:
            # Check if keyword is a JSON list
            parsed_json = json.loads(query.keyword)
            if isinstance(parsed_json, list):
                keywords_list = [str(k).strip() for k in parsed_json if str(k).strip()]
            else:
                keywords_list = [str(parsed_json).strip()]
        except Exception:
            # If not JSON, check if it's comma-separated or newline-separated
            if "," in query.keyword or "\n" in query.keyword:
                keywords_list = [k.strip() for k in re.split(r'[,\n]', query.keyword) if k.strip()]
            else:
                keywords_list = [query.keyword.strip()]

        # Ensure KeywordProgress records exist for all keywords in the query
        for kw in keywords_list:
            kp_record = db.scalars(select(KeywordProgress).where(
                KeywordProgress.search_query_id == search_id,
                KeywordProgress.keyword == kw
            )).first()
            if not kp_record:
                kp_record = KeywordProgress(
                    search_query_id=search_id,
                    keyword=kw,
                    status="pending",
                    articles_found=0
                )
                db.add(kp_record)
            else:
                kp_record.status = "pending"
                kp_record.articles_found = 0
                kp_record.started_at = None
                kp_record.completed_at = None
        db.commit()

        seen_content_hashes: Set[str] = {
            c.content_hash for c in db.scalars(select(CrawledURL).where(
                CrawledURL.search_id == search_id,
                CrawledURL.content_hash.isnot(None)
            )).all()
        }
        seen_simhashes: List[str] = []

        # Parse languages_filter from JSON once
        languages_filter = None
        if query.languages_filter:
            try:
                languages_filter = json.loads(query.languages_filter)
            except Exception as e:
                print(f"Error parsing languages_filter: {e}")

        # Pre-discover keyword-independent candidate URLs if source_type == "direct" or sitemap/feed
        pre_discovered_candidates = None
        pre_discovered_domains = None

        if query.source_type == "direct":
            raw_direct_urls = [line.strip() for line in (query.direct_urls or "").split("\n") if line.strip()]
            unique_raw_urls = list(dict.fromkeys(raw_direct_urls))
            print(f"[Direct Discovery] Running keyword-independent discovery for {len(unique_raw_urls)} unique URLs...")
            pre_discovered_candidates, pre_discovered_domains = run_direct_discovery(unique_raw_urls, query)
        elif query.source_type == "sitemap":
            from backend.sitemap_discovery import discover_from_sitemap
            base_url = (query.direct_urls or "").strip().splitlines()[0].strip()
            print(f"[Sitemap Discovery] Pre-discovering URLs from sitemap: {base_url} ...")
            sitemap_urls = list(dict.fromkeys(discover_from_sitemap(base_url, max_urls=500)))
            pre_discovered_candidates = {base_url: sitemap_urls}
            # Extract domain of sitemap
            parsed_base = urlparse(base_url)
            domain_val = parsed_base.netloc.lower()
            if domain_val.startswith("www."):
                domain_val = domain_val[4:]
            pre_discovered_domains = {domain_val}
        elif query.source_type == "feed":
            from backend.sitemap_discovery import discover_from_feeds
            base_url = (query.direct_urls or "").strip().splitlines()[0].strip()
            print(f"[Feed Discovery] Pre-discovering URLs from feed: {base_url} ...")
            feed_urls = list(dict.fromkeys(discover_from_feeds(base_url, max_urls=200)))
            pre_discovered_candidates = {base_url: feed_urls}
            # Extract domain of feed
            parsed_base = urlparse(base_url)
            domain_val = parsed_base.netloc.lower()
            if domain_val.startswith("www."):
                domain_val = domain_val[4:]
            pre_discovered_domains = {domain_val}

        # Parallel keyword processing
        # Keywords are independent; run them concurrently to eliminate N×serial cost.
        # Shared dedup state is protected by seen_lock.
        seen_lock = threading.Lock()
        import os
        _kw_max_workers = min(len(keywords_list), int(
            os.environ.get("KS_MAX_KEYWORD_WORKERS", "8")
        ))

        kw_pool = ThreadPoolExecutor(max_workers=_kw_max_workers, thread_name_prefix="KWWorker")
        try:
            kw_futures = {
                kw_pool.submit(
                    _process_single_keyword,
                    search_id=search_id,
                    kw=kw,
                    query=query,
                    languages_filter=languages_filter,
                    seen_content_hashes=seen_content_hashes,
                    seen_simhashes=seen_simhashes,
                    seen_lock=seen_lock,
                    total_keyword_count=len(keywords_list),
                    pre_discovered_candidates=pre_discovered_candidates,
                    pre_discovered_domains=pre_discovered_domains,
                ): kw
                for kw in keywords_list
                if not (_queue_stop_event.is_set() or is_job_stopped(search_id))
            }
            for fut in as_completed(kw_futures):
                if _queue_stop_event.is_set() or is_job_stopped(search_id):
                    for f in kw_futures:
                        f.cancel()
                    break
                kw_label = kw_futures[fut]
                try:
                    fut.result()
                except Exception as kw_err:
                    print(f"[ERROR] Keyword worker '{kw_label}' raised: {kw_err}")
        finally:
            wait_on_shutdown = not (is_job_stopped(search_id) or _queue_stop_event.is_set())
            kw_pool.shutdown(wait=wait_on_shutdown)

        # Clean up remaining pending URLs
        pending_urls = db.scalars(select(CrawledURL).where(
            CrawledURL.search_id == search_id,
            CrawledURL.status == "pending"
        )).all()
        for p_url in pending_urls:
            p_url.status = "skipped"
            p_url.error_message = "Crawl interrupted or skipped."
        if pending_urls:
            db.commit()

        # Recalculate and update total_urls_crawled
        crawled_count = db.execute(select(func.count(CrawledURL.id)).where(
            CrawledURL.search_id == search_id,
            CrawledURL.status != "pending"
        )).scalar() or 0
        matched_count = db.execute(select(func.count(CrawledURL.id)).where(
            CrawledURL.search_id == search_id,
            CrawledURL.status == "matched"
        )).scalar() or 0
        query.total_urls_crawled = crawled_count
        query.total_urls_matched = matched_count

        # Complete run if not stopped/aborted
        if is_job_stopped(search_id):
            query.status = "aborted"
            query.updated_at = datetime.now(timezone.utc)
            db.commit()
        elif not _queue_stop_event.is_set():
            query.status = "completed"
            query.updated_at = datetime.now(timezone.utc)
            db.commit()
            
            # Automatically synchronize matched records to PostgreSQL
            try:
                from backend.postgres_integration import export_search_to_postgres
                export_search_to_postgres(search_id, db)
                print(f"[PostgreSQL Auto-Sync] Successfully synchronized search {search_id} results.")
            except Exception as pg_err:
                print(f"[PostgreSQL Auto-Sync Warning] Failed to auto-sync results for search {search_id}: {pg_err}")
            
    except Exception as e:
        try:
            pending_urls = db.scalars(select(CrawledURL).where(
                CrawledURL.search_id == search_id,
                CrawledURL.status == "pending"
            )).all()
            for p_url in pending_urls:
                p_url.status = "failed"
                p_url.error_message = f"Queue worker crash: {str(e)}"
            db.commit()
            
            crawled_count = db.execute(select(func.count(CrawledURL.id)).where(
                CrawledURL.search_id == search_id,
                CrawledURL.status != "pending"
            )).scalar() or 0
            query.total_urls_crawled = crawled_count
        except Exception:
            pass
            
        query.status = "failed"
        query.error_message = f"Queue Worker Error: {str(e)}"
        query.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        try:
            with _shared_aborted_lock:
                _shared_aborted_jobs.pop(search_id, None)
        except Exception:
            pass
        db.close()

def queue_worker_loop():
    """Background worker loop polling for pending jobs."""
    while not _queue_stop_event.is_set():
        db = SessionLocal()
        try:
            # Query for first pending task
            pending_query = db.scalars(select(SearchQuery).where(SearchQuery.status == "pending")).first()
            if pending_query:
                db.close()  # Close session before starting heavy thread process
                try:
                    process_search_query(pending_query.id)
                except Exception as e:
                    # Safety net to recover query state in case of worker crash
                    recovery_db = SessionLocal()
                    try:
                        q = recovery_db.scalars(select(SearchQuery).where(SearchQuery.id == pending_query.id)).first()
                        if q and q.status not in ("completed", "failed"):
                            q.status = "failed"
                            q.error_message = f"Unhandled worker crash: {str(e)}"
                            q.updated_at = datetime.now(timezone.utc)
                            recovery_db.commit()
                    except Exception as commit_err:
                        print(f"Error recovering query in queue safety net: {commit_err}")
                    finally:
                        recovery_db.close()
            else:
                db.close()
                time.sleep(2.0)  # Wait 2 seconds before polling again
        except Exception as e:
            print(f"Error in queue worker loop: {e}")
            try:
                db.close()
            except Exception:
                pass
            time.sleep(5.0)

def start_queue_worker():
    """Starts the queue manager background thread."""
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _queue_stop_event.clear()
        _shared_process_stop_event.clear()
        _worker_thread = threading.Thread(target=queue_worker_loop, name="QueueWorkerThread", daemon=True)
        _worker_thread.start()
        print("Queue Worker Thread started successfully.")

def stop_queue_worker():
    """Stops the queue manager background thread."""
    global _worker_thread
    _queue_stop_event.set()
    _shared_process_stop_event.set()
    if _worker_thread:
        _worker_thread.join(timeout=10)
        _worker_thread = None
        print("Queue Worker Thread stopped.")
