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
import os
import multiprocessing
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Set, Tuple, Any, Optional
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
from sqlalchemy.orm import Session
from sqlalchemy import update, select, func
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from backend.database import SessionLocal
from backend.models import SearchQuery, CrawledURL, KeywordProgress
from backend.search_engine import search_web
from backend.crawler import Crawler
from bs4 import BeautifulSoup
from backend.site_search_detector import SiteSearchDetector
from backend.url_classifier import filter_candidate_urls, is_chinese_url
# Import the strategy submodules (not just `base`) so their @register decorators
# actually run and populate base._REGISTRY - without this, base.get_strategy()
# returns None for every profile and every site silently falls back to legacy
# link-expansion discovery, defeating the entire adaptive discovery layer.
from backend.discovery import (
    base as discovery_base,
    wp_api, oai_pmh, sitemap, news_sitemap, site_search, deep_crawl,
)
from backend.discovery.base import DiscoveredURL

# Thread-safe shared state variables
_shared_aborted_jobs = {}
_shared_aborted_lock = threading.Lock()
_shared_domain_last_crawl = {}
_shared_domain_lock = threading.Lock()
_shared_process_stop_event = threading.Event()
# Job-wide cap on TOTAL concurrently-executing crawl_url_task fetch+parse work,
# regardless of how many keywords run concurrently or how large each keyword's
# own ThreadPoolExecutor is sized. Nothing previously capped this: per-keyword
# pool size (~15 workers) x concurrent keywords (~8) multiplied unboundedly
# toward ~120+ simultaneous Python threads. Confirmed via direct reproduction
# that Python's GIL cannot service that many concurrent threads doing real
# I/O+CPU-bound work (HTML parsing, language detection, hashing) efficiently:
# 120 threads completed only 13% of a real 505-URL batch within 130s; the SAME
# batch at 30 threads completed 29% with ZERO individual tasks exceeding the
# crawl watchdog. 25 is a conservative middle value from that data.
_CRAWL_CONCURRENCY_SEMAPHORE = threading.BoundedSemaphore(
    int(os.environ.get("KS_MAX_CONCURRENT_CRAWLS", "25"))
)

# ── Phase 1: process pool for Crawler._analyze_page_impl (CPU-bound analysis) ──
# analyze_page() (HTML re-parsing x2-3, language classification, trafilatura
# metadata) is 100% CPU-bound with zero network I/O - confirmed by reading the
# method body - and was identified as a strong candidate for the dominant GIL
# contention source under this module's ThreadPoolExecutor-based crawl pipeline
# (threads doing a mix of network I/O and CPU-bound work can't get real
# parallelism on CPU work under the GIL). Offloading it to a ProcessPoolExecutor
# gets true parallelism across this host's many real cores. See
# crawl_url_task()'s analysis call site for how this is used, and
# backend/crawler.py's `_analyze_page_impl` (module-level, picklable by
# reference - required since ProcessPoolExecutor can't pickle bound methods)
# for the function being offloaded.
_ANALYSIS_POOL: Optional[ProcessPoolExecutor] = None
_ANALYSIS_POOL_LOCK = threading.Lock()
# Conservative default (12, not the full 48-core host) - each worker process
# imports trafilatura + py3langid (+ optionally selenium/playwright at module
# import time), a real memory floor per process. Tune up only from measured
# Phase 0/1 data, not guessed at the host's full core count upfront.
_ANALYSIS_POOL_WORKERS = int(os.environ.get("KS_ANALYSIS_POOL_WORKERS", "12"))
# Recycle workers periodically (Python 3.12 feature). ProcessPoolExecutor
# futures cannot be force-cancelled once running, so a single pathological
# hang (e.g. a page that sends trafilatura into a bad regex-backtracking case)
# would otherwise permanently consume one worker for the rest of the process's
# lifetime. 50 tasks/worker bounds that damage without recycling so often that
# per-worker import cost (trafilatura/py3langid) dominates.
_ANALYSIS_POOL_MAX_TASKS_PER_CHILD = int(os.environ.get("KS_ANALYSIS_POOL_MAX_TASKS_PER_CHILD", "50"))
# Bound on how long we wait for a single analysis result before treating it
# like any other analysis failure (existing "Parsing Error" path in
# crawl_url_task - no new failure semantics). Comfortably under the 120s crawl
# watchdog, leaving room for the fetch phase that already happened.
_ANALYSIS_RESULT_TIMEOUT_S = float(os.environ.get("KS_ANALYSIS_TIMEOUT_S", "60"))


def _get_analysis_pool() -> ProcessPoolExecutor:
    """
    Lazily creates (or returns the existing) module-level ProcessPoolExecutor
    used to offload Crawler._analyze_page_impl. Uses multiprocessing's "spawn"
    start method explicitly - NOT the platform default (fork on Linux) -
    because this parent process holds live resources (SQLAlchemy engine
    connection pool, Selenium/webdriver state, VPN/Tor router state) that must
    not be duplicated into a child via fork()'s copy-on-write semantics; spawn
    starts each worker as a fresh interpreter that only imports what it needs.
    """
    global _ANALYSIS_POOL
    with _ANALYSIS_POOL_LOCK:
        if _ANALYSIS_POOL is None:
            ctx = multiprocessing.get_context("spawn")
            _ANALYSIS_POOL = ProcessPoolExecutor(
                max_workers=_ANALYSIS_POOL_WORKERS,
                mp_context=ctx,
                max_tasks_per_child=_ANALYSIS_POOL_MAX_TASKS_PER_CHILD,
            )
        return _ANALYSIS_POOL


def _reset_analysis_pool() -> None:
    """Tears down a broken analysis pool so the next _get_analysis_pool() call
    creates a fresh one. Called after a BrokenProcessPoolError so subsequent
    tasks don't keep hitting the same dead pool."""
    global _ANALYSIS_POOL
    with _ANALYSIS_POOL_LOCK:
        pool, _ANALYSIS_POOL = _ANALYSIS_POOL, None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def _run_analysis(html_content: str, url: str, keyword: str, match_type: str,
                   case_sensitive: bool, exact_match: bool) -> Dict[str, Any]:
    """
    Runs Crawler._analyze_page_impl (CPU-bound HTML/keyword analysis) in the
    shared analysis process pool, bypassing the GIL for real parallelism.
    Falls back to an in-thread synchronous call for THIS task only if the pool
    itself is broken (e.g. a worker crashed hard) - the pool is recreated so
    later tasks get a working pool again, rather than losing work.
    """
    from backend.crawler import _analyze_page_impl
    try:
        pool = _get_analysis_pool()
        future = pool.submit(
            _analyze_page_impl, html_content, url, keyword, match_type, case_sensitive, exact_match
        )
        return future.result(timeout=_ANALYSIS_RESULT_TIMEOUT_S)
    except BrokenProcessPool as bppe:
        print(f"[AnalysisPool] Pool broken ({bppe}); recreating and falling back to "
              f"in-thread analysis for this task only.")
        _reset_analysis_pool()
        return _analyze_page_impl(html_content, url, keyword, match_type, case_sensitive, exact_match)


# Per-domain locks for _reserve_domain_slot()'s slot-reservation critical section -
# separate from _shared_domain_lock, which now only guards brief get-or-create of
# an entry here. See _reserve_domain_slot()'s docstring for why.
_shared_domain_slot_locks: Dict[str, threading.Lock] = {}

# Fallback structures for non-multiprocessed local tasks
DOMAIN_LAST_CRAWL: Dict[str, float] = {}
domain_lock = threading.Lock()
_domain_slot_locks: Dict[str, threading.Lock] = {}


def _get_domain_slot_lock(domain: str, registry: Dict[str, threading.Lock],
                           meta_lock: threading.Lock) -> threading.Lock:
    """Get-or-create a per-domain lock in `registry`, guarded briefly by
    `meta_lock` only for the creation itself - not held during actual use."""
    lock = registry.get(domain)
    if lock is not None:
        return lock
    with meta_lock:
        lock = registry.get(domain)
        if lock is None:
            lock = threading.Lock()
            registry[domain] = lock
        return lock


def _reserve_domain_slot(domain: str, rate_limit_s: float,
                          last_crawl_registry: Dict[str, float],
                          slot_lock_registry: Dict[str, threading.Lock],
                          meta_lock: threading.Lock,
                          check_stopped) -> bool:
    """
    Reserves the next available crawl-fetch slot for `domain` and sleeps until it
    arrives, WITHOUT busy-wait polling. Returns False if `check_stopped()` fires
    during the wait (caller should treat the task as aborted), True otherwise.

    Replaces a previous while-True busy-wait design where every thread wanting a
    domain's turn repeatedly re-acquired ONE GLOBAL lock (shared across every
    domain in the whole job, not just this one) to check-then-sleep-then-recheck.
    Under real concurrent load (up to ~150 threads across several keyword pools,
    several legitimately sharing the same domains) that created severe GIL/lock
    contention - confirmed live: a pilot run left ~84% of tasks failing a 120s
    watchdog despite a fully idle 48-core host and sub-2-second direct response
    times from the exact same domains, which rules out both hardware and network
    as the cause.

    This design: each thread computes and reserves its OWN unique slot time in a
    single brief PER-DOMAIN critical section (`max(now, previous_slot +
    rate_limit_s)`), then sleeps ONCE for exactly its own precomputed duration -
    no repeated lock re-acquisition, and threads waiting on DIFFERENT domains
    never contend with each other at all (the shared meta_lock is only held for
    the brief get-or-create of a per-domain lock, not for the reservation math or
    the sleep). Preserves the original semantic of rate-limiting request
    INITIATION timing, not full serialization - a slow fetch doesn't block the
    next slot from being reserved.
    """
    slot_lock = _get_domain_slot_lock(domain, slot_lock_registry, meta_lock)
    with slot_lock:
        now = time.time()
        last_slot = last_crawl_registry.get(domain, 0.0)
        slot_time = max(now, last_slot + rate_limit_s)
        last_crawl_registry[domain] = slot_time
    end = slot_time
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return True
        if check_stopped():
            return False
        time.sleep(min(0.5, remaining))

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


def is_news_article_url(url: str) -> bool:
    """
    Heuristic to determine if a URL represents a news article page (not a homepage or category index).
    """
    from urllib.parse import urlparse
    import re
    
    parsed = urlparse(url)
    path = parsed.path.lower()
    path_clean = path.strip('/')
    
    # Homepage check
    if not path_clean:
        return False
        
    # Short index files
    if path_clean in ("index.html", "index.shtml", "index.htm", "default.html", "default.shtml"):
        return False
        
    # Category paths
    parts = path_clean.split('/')
    
    # If the path has no directory/file structure or is extremely short, check if it has digits
    has_digits = any(c.isdigit() for c in parts[-1]) if parts else False
    
    # Check date patterns in path: /2026/07/27/, /2026-07-27/, /202607/ etc.
    has_date_in_path = bool(re.search(r'/\d{4}[-/]?\d{2}[-/]?\d{2}/|/\d{4}\d{2}/|/page/\d{6}/|/a/\d{6}/', path))
    
    # Common article extensions
    ends_with_html = any(path_clean.endswith(ext) for ext in (".html", ".shtml", ".htm"))
    
    # If it clearly has a publication date pattern in the path
    if has_date_in_path:
        return True
        
    # FMPRC statements format (e.g. t20260727_11234567.shtml)
    if bool(re.search(r't\d{8}_', path)):
        return True

    # If it is a category index page (e.g. /world, /world/, /opinion)
    if len(parts) <= 2 and not has_digits and not has_date_in_path:
        return False
        
    # Deep file paths ending in typical page extensions with digits in the filename
    if len(parts) >= 2 and (has_digits or ends_with_html):
        return True
        
    return False


def extract_date_from_url(url: str) -> Optional[datetime]:
    """
    Extracts a publication date from the URL path as a fallback.
    """
    from urllib.parse import urlparse
    import re
    
    parsed = urlparse(url)
    path = parsed.path.lower()
    
    # 1. Match YYYY/MM/DD or YYYY-MM-DD
    m = re.search(r'/(\d{4})[-/](\d{2})[-/](\d{2})/', path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass
            
    # 2. Match YYYY/MM or YYYY-MM
    m = re.search(r'/(\d{4})[-/](\d{2})/', path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc)
        except ValueError:
            pass

    # 3. Match /YYYYMM/
    m = re.search(r'/(\d{4})(\d{2})/', path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc)
        except ValueError:
            pass

    # 4. Match /a/YYYYMM/
    m = re.search(r'/a/(\d{4})(\d{2})/', path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc)
        except ValueError:
            pass
            
    # 5. Match tYYYYMMDD_
    m = re.search(r't(\d{4})(\d{2})(\d{2})_', path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


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
    _sem_acquired = False
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

        # Retrieve source_type from search query details
        query_record = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
        source_type = query_record.source_type if query_record else "direct"

        # Close DB session early and safely now that we have read the attributes
        db.close()
        db = None

        # Apply article-only check if no-keyword config run is active
        is_keyword_free = not keyword or not keyword.strip() or keyword == "__config__"
        is_no_keyword_config = (source_type == "direct" and is_keyword_free)
        if is_no_keyword_config:
            if not is_news_article_url(url):
                return url_id, {
                    "status": "skipped",
                    "error_message": "Skipped: Homepage or category/index landing page content."
                }

        # Resolve effective rate limit (env override → passed arg → hardcoded default)
        import os as _os
        if domain_rate_limit is None:
            try:
                domain_rate_limit = float(_os.environ.get("KS_DOMAIN_RATE_LIMIT", "1.0"))
            except (TypeError, ValueError):
                domain_rate_limit = 1.0

        # 1. Enforce Domain Rate Limiting - see _reserve_domain_slot()'s docstring
        # for why this no longer busy-waits on one lock shared across every domain.
        if shared_domain_last_crawl_dict is not None and shared_domain_lock is not None:
            last_crawl_registry = shared_domain_last_crawl_dict
            meta_lock = shared_domain_lock
            slot_lock_registry = _shared_domain_slot_locks
        else:
            last_crawl_registry = DOMAIN_LAST_CRAWL
            meta_lock = domain_lock
            slot_lock_registry = _domain_slot_locks

        if not _reserve_domain_slot(domain, domain_rate_limit, last_crawl_registry,
                                     slot_lock_registry, meta_lock, check_stopped):
            return url_id, {"status": "skipped", "error_message": "Job aborted by user."}

        # Job-wide cap on concurrent fetch+parse work - see
        # _CRAWL_CONCURRENCY_SEMAPHORE's definition for why. Released in this
        # function's outer `finally:` block below via the _sem_acquired flag, so
        # it's freed on every exit path (success, exception, or early return).
        _CRAWL_CONCURRENCY_SEMAPHORE.acquire()
        _sem_acquired = True

        crawler = Crawler(proxy_url=proxy_url)

        # 2. Fetch page with retries (up to 2 retries, exponential backoff)
        max_retries = 2
        retry_delay = 1.0
        html_content = ""

        # Phase 0 instrumentation: measure fetch-wait-time (network I/O, including
        # retry backoff sleeps) separately from analysis-time (CPU-bound HTML
        # parsing/classification) so the two can be compared without conflating
        # them - see _run_analysis()/_get_analysis_pool() above for why this
        # split matters (only analysis is CPU-bound and thus GIL-contending).
        _fetch_start = time.perf_counter()
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
        fetch_s = time.perf_counter() - _fetch_start

        crawler.close()

        # 3. Analyze page content if fetch was successful
        if html_content:
            try:
                effective_keyword = "" if keyword == "__config__" else keyword
                # Phase 1: offload the CPU-bound analysis to a ProcessPoolExecutor
                # instead of calling crawler.analyze_page(...) in-thread - see
                # _run_analysis()'s docstring above. analyze_s below includes the
                # pool round-trip (submit + IPC serialization + .result() wait),
                # not just raw compute time, since that round-trip cost is itself
                # part of what Phase 0/1 needs to measure (raw_html/full_content
                # are large strings crossing the process boundary both ways).
                _analyze_start = time.perf_counter()
                analysis = _run_analysis(
                    html_content, url, effective_keyword, match_type, case_sensitive, exact_match
                )
                analyze_s = time.perf_counter() - _analyze_start
                print(f"[CrawlTiming] url_id={url_id} domain={domain} "
                      f"fetch_s={fetch_s:.3f} analyze_s={analyze_s:.3f}")
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
                if not pub_date or not isinstance(pub_date, datetime):
                    pub_date = extract_date_from_url(url)

                if is_no_keyword_config:
                    if not pub_date:
                        result["status"] = "skipped"
                        result["error_message"] = "Skipped: Unable to verify article publication date."
                    else:
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                        three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
                        if pub_date < three_months_ago:
                            result["status"] = "skipped"
                            result["error_message"] = f"Skipped: Article published more than 3 months ago ({pub_date.strftime('%Y-%m-%d')})."
                        else:
                            result["status"] = "matched"

                if result["status"] == "matched" and pub_date and isinstance(pub_date, datetime):
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
        else:
            # Fetch never produced content (failed/skipped/aborted) - still log
            # fetch_s so Phase 0 aggregation isn't biased toward only-successful
            # fetches; analyze_s is naturally absent since analysis never ran.
            print(f"[CrawlTiming] url_id={url_id} domain={domain} "
                  f"fetch_s={fetch_s:.3f} analyze_s=NA")

    finally:
        if _sem_acquired:
            _CRAWL_CONCURRENCY_SEMAPHORE.release()
        if db is not None:
            db.close()
    return url_id, result

_SITE_PROFILES_CACHE = None


def _load_site_profiles():
    global _SITE_PROFILES_CACHE
    if _SITE_PROFILES_CACHE is None:
        try:
            with open("config/site_profiles.json") as f:
                _SITE_PROFILES_CACHE = json.load(f).get("profiles", {})
        except Exception as e:
            print(f"[SiteProfiles] Failed to load config/site_profiles.json: {e}")
            _SITE_PROFILES_CACHE = {}
    return _SITE_PROFILES_CACHE


def run_direct_discovery(
    unique_raw_urls: List[str], query, keyword: Optional[str] = None,
    domain_candidate_budget: Optional[Dict[str, int]] = None,
    domain_budget_lock: Optional[threading.Lock] = None,
) -> Tuple[Dict[str, List[DiscoveredURL]], Set[str]]:
    """
    Runs URL discovery once per unique URL, dispatching to the per-site adaptive
    discovery layer (config/site_profiles.json) when a profile is registered for
    the domain, and falling back to the legacy sitemap/feed/link-expansion logic
    otherwise (or when a profile requires a keyword that wasn't supplied).

    Every candidate URL, from every code path, is wrapped in a DiscoveredURL so the
    metadata it carries (published_at, title, relevance_score) survives to the
    caller instead of being flattened back to a bare string. That metadata is what
    lets stale/irrelevant URLs get dropped before a fetch is attempted.

    `domain_candidate_budget`/`domain_budget_lock` (optional, shared with
    _process_single_keyword's callers - see process_search_query()) cap the
    primary-discovery branch's own contribution per domain via
    _trim_discovered_to_domain_budget(). Without this, a profile-driven strategy
    that genuinely succeeds (e.g. a sitemap-strategy site with a large recent
    surface) can return hundreds of candidates for one domain, completely
    unbounded by the budget mechanism that otherwise gates
    search_web()/SiteSearchDetector - confirmed live: newslivetv.com alone
    returned 309 candidates this way in one 61-source run. No-op (unbounded, as
    before) if either is None - only real caller today is process_search_query()
    / _process_single_keyword()'s fallback branch, both of which always supply
    both.

    Returns:
      - A dict mapping d_url -> List[DiscoveredURL]
      - A set of bare domains of the direct URLs
    """
    discovered_candidates = {}
    searched_domains = set()

    def _wrap_legacy(urls: List[str]) -> List[DiscoveredURL]:
        wrapped = []
        for u in urls:
            try:
                wrapped.append(DiscoveredURL(url=u, source="legacy"))
            except ValueError:
                continue
        return wrapped

    def _discover_single(d_url: str) -> Tuple[str, List[DiscoveredURL], Optional[str]]:
        if is_job_stopped(query.id):
            return d_url, [], None

        if not (d_url.startswith("http://") or d_url.startswith("https://")):
            return d_url, [], None

        parsed = urlparse(d_url)
        path = parsed.path.lower()

        # Get bare domain
        od = parsed.netloc.lower()
        domain = od[4:] if od.startswith("www.") else od

        # Adaptive discovery: profile-driven strategy dispatch
        profile = _load_site_profiles().get(domain)
        if profile is not None:
            strategy = discovery_base.get_strategy(profile)
            use_profile = strategy is not None
            # A `search`-strategy site needs a keyword to return anything (its discover()
            # immediately errors out "requires a keyword; none given" otherwise). This
            # function is sometimes called keyword-independently, so in that case fall
            # through to the legacy path rather than getting an empty result for every
            # site that uses site-native search.
            if use_profile and profile.get("strategy") == "search" and not keyword:
                use_profile = False
            if use_profile:
                import os as _os_dd
                max_urls = int(_os_dd.environ.get("KS_MAX_CANDIDATE_URLS", "500"))
                effective_since = query.date_range_start
                if effective_since is None:
                    from datetime import timedelta as _timedelta
                    effective_since = datetime.now(timezone.utc) - _timedelta(days=90)
                if effective_since.tzinfo is None:
                    effective_since = effective_since.replace(tzinfo=timezone.utc)
                result = strategy.discover(keyword=keyword, max_urls=max_urls, since=effective_since)
                for err in result.errors:
                    print(f"[Discovery:{profile.get('strategy')}] {domain}: {err}")
                trimmed_urls = _trim_discovered_to_domain_budget(
                    domain, result.urls, domain_candidate_budget, domain_budget_lock)
                if len(trimmed_urls) < len(result.urls):
                    print(f"[Discovery:{profile.get('strategy')}] {domain}: trimmed "
                          f"{len(result.urls)} -> {len(trimmed_urls)} candidates "
                          f"(shared per-domain budget)")
                return d_url, trimmed_urls, domain

        # Sitemap check
        if path.endswith(".xml") or "sitemap" in path:
            try:
                from backend.sitemap_discovery import discover_from_sitemap
                if is_job_stopped(query.id):
                    return d_url, [], domain
                urls = discover_from_sitemap(d_url, max_urls=500)
                print(f"[SitemapDiscovery] Found {len(urls)} URLs for sitemap: {d_url}")
                return d_url, _wrap_legacy(urls), domain
            except Exception as e:
                print(f"[SitemapDiscovery] Failed for {d_url}: {e}")
                return d_url, _wrap_legacy([d_url]), domain

        # Feed check
        elif "rss" in path or "feed" in path or "atom" in path or path.endswith("/feed") or path.endswith("/feed/"):
            try:
                from backend.sitemap_discovery import discover_from_feeds
                if is_job_stopped(query.id):
                    return d_url, [], domain
                urls = discover_from_feeds(d_url, max_urls=200)
                print(f"[FeedDiscovery] Found {len(urls)} URLs for feed: {d_url}")
                return d_url, _wrap_legacy(urls), domain
            except Exception as e:
                print(f"[FeedDiscovery] Failed for {d_url}: {e}")
                return d_url, _wrap_legacy([d_url]), domain

        # Regular URL -> link expansion
        else:
            candidates = [d_url]
            local_crawler = Crawler(proxy_url=getattr(query, 'proxy_url', None))
            try:
                if is_job_stopped(query.id):
                    return d_url, _wrap_legacy([d_url]), domain
                html_text = local_crawler.fetch_page(d_url, engine="fast", ignore_robots=getattr(query, 'ignore_robots', False))
                if is_job_stopped(query.id):
                    return d_url, _wrap_legacy([d_url]), domain
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
            return d_url, _wrap_legacy(candidates), domain

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
                        # Dedup by URL, not by object identity - DiscoveredURL is an
                        # unhashable dataclass (default eq=True disables __hash__).
                        seen_urls = set()
                        deduped = []
                        for du in candidates:
                            if du.url not in seen_urls:
                                seen_urls.add(du.url)
                                deduped.append(du)
                        discovered_candidates[d_url] = deduped
                        if domain:
                            searched_domains.add(domain)
                    except Exception as ex:
                        print(f"[WARNING] Direct URL discovery future failed: {ex}")
        finally:
            wait_on_shutdown = not is_job_stopped(query.id)
            exp_pool.shutdown(wait=wait_on_shutdown)

    return discovered_candidates, searched_domains

from types import SimpleNamespace

# Fields read (never written) off the `query` SearchQuery row from within
# discovery/keyword worker threads. Kept in sync with every `query.<attr>` /
# `getattr(query, ...)` read inside _process_single_keyword() and
# run_direct_discovery().
import os as _os_qm

# Cumulative cap on candidates a single domain can contribute via site-restricted
# web search across the WHOLE job (all keywords combined) - see the long comment
# where this is applied for why: search_web() runs once per keyword per domain
# and returns genuinely distinct real results each time, so nothing upstream
# dedupes it, and per-domain crawl-fetch rate limiting is shared/serialized at
# ~1 req/sec regardless of how many candidates are queued.
_MAX_CANDIDATES_PER_DOMAIN = int(_os_qm.environ.get("KS_MAX_CANDIDATES_PER_DOMAIN", "100"))


def _trim_to_domain_budget(url: str, candidates: List[str],
                            domain_candidate_budget: Optional[Dict[str, int]],
                            domain_budget_lock: Optional[threading.Lock]) -> List[str]:
    """Trims `candidates` (freshly discovered for `url`'s domain by a per-keyword
    mechanism - site-restricted web search or SiteSearchDetector) against the
    domain's remaining share of _MAX_CANDIDATES_PER_DOMAIN for the whole job.

    Both of those discovery mechanisms run once PER KEYWORD and return genuinely
    distinct real URLs each time (not duplicates), so nothing upstream dedupes
    across keywords - a "hot" domain's total candidate count is otherwise
    unbounded by keyword count. Per-domain crawl-fetch rate limiting is shared/
    serialized (~1 req/sec by default): a domain with N queued candidates makes
    its last-queued task wait ~N seconds for a rate-limit turn, so bounding N
    keeps that within the crawl-task watchdog's reach. No-op (returns candidates
    unchanged) if the budget dict/lock weren't supplied - only real caller today
    is process_search_query(), which always supplies both.
    """
    if domain_candidate_budget is None or domain_budget_lock is None or not candidates:
        return candidates
    domain = (urlparse(url).netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain:
        return candidates
    with domain_budget_lock:
        used = domain_candidate_budget.get(domain, 0)
        remaining = max(0, _MAX_CANDIDATES_PER_DOMAIN - used)
        trimmed = candidates[:remaining] if remaining < len(candidates) else candidates
        domain_candidate_budget[domain] = used + len(trimmed)
    return trimmed


def _trim_discovered_to_domain_budget(domain: str, candidates: List[DiscoveredURL],
                                       domain_candidate_budget: Optional[Dict[str, int]],
                                       domain_budget_lock: Optional[threading.Lock]) -> List[DiscoveredURL]:
    """Same shared per-domain budget as _trim_to_domain_budget(), but for
    run_direct_discovery()'s primary-discovery branch (_discover_single), which
    operates on List[DiscoveredURL] rather than List[str] and already has
    `domain` resolved in scope (from the parsed d_url) rather than needing it
    re-derived per-candidate.

    Fix for the gap documented in HANDOFF.md item #2: primary per-site
    discovery (a profile-driven strategy.discover() call) was completely
    uncapped by the budget mechanism that gates search_web()/SiteSearchDetector
    - confirmed live, a single domain (newslivetv.com) returned 309 candidates
    this way in one 61-source run. No-op (returns candidates unchanged) if the
    budget dict/lock weren't supplied, or `domain` is falsy.
    """
    if domain_candidate_budget is None or domain_budget_lock is None or not candidates:
        return candidates
    if not domain:
        return candidates
    with domain_budget_lock:
        used = domain_candidate_budget.get(domain, 0)
        remaining = max(0, _MAX_CANDIDATES_PER_DOMAIN - used)
        trimmed = candidates[:remaining] if remaining < len(candidates) else candidates
        domain_candidate_budget[domain] = used + len(trimmed)
    return trimmed


def _fair_trim_candidates_across_domains(candidate_urls: List[str], max_candidates: int,
                                          get_domain) -> List[str]:
    """Fair, round-robin-across-domains trim of a combined, post-dedup
    candidate_urls list down to max_candidates.

    Fixes the sibling bug to the one _trim_to_domain_budget()/
    _trim_discovered_to_domain_budget() address: even when every individual
    domain's OWN contribution is within budget, _process_single_keyword's final
    "Candidate cap" combines ALL domains' candidates for one keyword into a
    single list before this trim runs. A naive candidate_urls[:max_candidates]
    keeps whichever domains happen to appear earliest in list-insertion order
    (primary discovery iterates raw_direct_urls in config/urls.json's source
    order) or happen to have contributed the most - at 61-source scale this let
    a handful of early/large domains crowd out every other domain's candidates
    entirely for a keyword, even domains whose own per-domain count was
    individually reasonable. Confirmed live (search_id=144): SiteSearchDetector
    found substantial real candidate counts for dozens of domains, but only 9 of
    61 domains ended up with ANY row in the final CrawledURL table for the whole
    job (found=501, matched=0).

    `get_domain` is injected (the caller's own already-defined get_domain(url)
    closure) rather than re-implemented here, per the existing convention of not
    duplicating that logic.

    Extracted as a standalone, directly-testable helper rather than left inline,
    so its fairness property (every domain gets at least one slot when
    len(candidate_urls) > max_candidates and there are <= max_candidates
    distinct domains) can be asserted directly in a unit test.
    """
    if len(candidate_urls) <= max_candidates:
        return candidate_urls
    from collections import defaultdict, deque
    by_domain: Dict[str, Any] = defaultdict(deque)
    for u in candidate_urls:  # preserve each domain's own original relative order
        by_domain[get_domain(u)].append(u)
    fair_trimmed: List[str] = []
    domain_queues = list(by_domain.values())
    while len(fair_trimmed) < max_candidates and domain_queues:
        next_round = []
        for q in domain_queues:
            if not q:
                continue
            fair_trimmed.append(q.popleft())
            if len(fair_trimmed) >= max_candidates:
                break
            next_round.append(q)
        domain_queues = next_round
    return fair_trimmed


_QUERY_SNAPSHOT_FIELDS = (
    "id", "source_type", "direct_urls", "domains_filter", "engine",
    "proxy_url", "ignore_robots", "date_range_start", "date_range_end",
    "match_type", "case_sensitive", "exact_match", "keyword", "languages_filter",
)


def _snapshot_query(query) -> SimpleNamespace:
    """
    Returns a plain, detached, thread-safe snapshot of the read-only fields that
    discovery/keyword worker threads need from a SearchQuery row.

    Root cause this exists to fix: process_search_query() fetches `query` ONCE
    from its own `db` Session, then hands that same live ORM object to every
    concurrent keyword thread (via ThreadPoolExecutor) and to
    run_direct_discovery()'s internal worker pool. SQLAlchemy Sessions expire
    all attributes on every commit() by default (expire_on_commit=True) - so the
    next attribute read on `query` after any commit silently triggers a fresh
    SELECT through that same Session/connection to refresh it. Sessions are not
    thread-safe: two threads reading `query.<attr>` at once (or one reading while
    the main thread is mid-commit on that same `db`) collide, and SQLAlchemy
    raises "This session is provisioning a new connection; concurrent operations
    are not permitted".

    Taking one plain-object snapshot before any threads are spawned, and handing
    *that* to every worker instead of the live ORM object, removes the shared
    session-bound state entirely - worker-thread reads never touch the Session
    again. The live `query` object stays owned by process_search_query()'s main
    thread alone, for writes (status/status_message/counters) and commits.
    """
    return SimpleNamespace(**{field: getattr(query, field) for field in _QUERY_SNAPSHOT_FIELDS})


def _process_single_keyword(
    search_id: int,
    kw: str,
    query,
    languages_filter,
    seen_content_hashes: set,
    seen_simhashes: list,
    seen_lock: threading.Lock,
    total_keyword_count: int,
    pre_discovered_candidates: Dict[str, List[DiscoveredURL]] = None,
    pre_discovered_domains: Set[str] = None,
    discover_only: bool = False,
    crawl_only: bool = False,
    chinese_only: Optional[bool] = None,
    discovered_urls: Optional[List[str]] = None,
    mark_completed: bool = True,
    domain_candidate_budget: Optional[Dict[str, int]] = None,
    domain_budget_lock: Optional[threading.Lock] = None,
) -> List[str]:
    """
    Processes a single keyword within a search job: discovers candidate URLs,
    crawls them, deduplicates results, and writes to DB.
    Designed to run concurrently with other keywords via ThreadPoolExecutor.
    """
    db = SessionLocal()
    # Bound before the try block so the final `return candidate_urls` (and the
    # `except` handler's `if kp_record:` check) are always well-defined, even if
    # an exception hits before Step 1 assigns candidate_urls or before kp_record
    # is fetched below. Previously these were only assigned deep inside the try
    # body, so an early failure (e.g. a DB session error) produced a confusing
    # secondary `UnboundLocalError` on the way out instead of a clean failure.
    candidate_urls: List[str] = []
    kp_record = None
    try:
        if _queue_stop_event.is_set() or is_job_stopped(search_id):
            return []

        kp_record = db.scalars(select(KeywordProgress).where(
            KeywordProgress.search_query_id == search_id,
            KeywordProgress.keyword == kw
        )).first()

        if kp_record and kp_record.status == "completed":
            print(f"Skipping completed keyword '{kw}' for search {search_id}")
            return []

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
        # Side-table preserving DiscoveredURL metadata (published_at, title,
        # relevance_score) keyed by URL string, since candidate_urls itself stays a
        # flat List[str] for all the downstream filtering/dedup/DB logic below.
        url_metadata: Dict[str, DiscoveredURL] = {}

        def _extend_from_discovered(d_url, mapping):
            items = mapping.get(d_url)
            if items is None:
                candidate_urls.append(d_url)
                return
            for du in items:
                candidate_urls.append(du.url)
                url_metadata[du.url] = du

        if crawl_only and discovered_urls is not None:
            candidate_urls = list(dict.fromkeys(discovered_urls))
        elif query.source_type == "direct":
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
                                        discovered_urls = _trim_to_domain_budget(
                                            d_url, discovered_urls,
                                            domain_candidate_budget, domain_budget_lock)
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
                        _extend_from_discovered(d_url, pre_discovered_candidates)

                # Add the site_search_urls
                candidate_urls.extend(site_search_urls)
                
            else:
                # Fallback implementation
                raw_direct_urls = [line.strip() for line in (query.direct_urls or "").split("\n") if line.strip()]
                unique_raw_urls = list(dict.fromkeys(raw_direct_urls))
                
                temp_candidates, searched_domains = run_direct_discovery(
                    unique_raw_urls, query,
                    keyword=(kw if kw.strip() and kw != "__config__" else None),
                    domain_candidate_budget=domain_candidate_budget,
                    domain_budget_lock=domain_budget_lock,
                )
                
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
                                        discovered_urls = _trim_to_domain_budget(
                                            d_url, discovered_urls,
                                            domain_candidate_budget, domain_budget_lock)
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
                        _extend_from_discovered(d_url, temp_candidates)
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
                # Track which domain each future is for, so results can be trimmed
                # against domain_candidate_budget below (a shared, cross-keyword,
                # cross-thread cap - see its creation in process_search_query()).
                sr_future_domain = {}
                sr_futures = []
                for d in searched_domains:
                    f = sr_pool.submit(_site_restricted_search, d)
                    sr_future_domain[f] = d
                    sr_futures.append(f)
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
                                results = fut.result()
                                # Site-restricted web search is the mechanism that
                                # inflates a single domain's candidate count: it runs
                                # independently PER KEYWORD (unlike structured
                                # discovery, which is computed once and shared), and
                                # each keyword's query genuinely returns different
                                # real articles - so nothing upstream deduplicates
                                # this. A domain's cumulative candidates across the
                                # WHOLE job can reach hundreds even though per-domain
                                # crawl-fetch rate limiting is a shared, serialized
                                # ~1 req/sec - meaning late-queued candidates on a
                                # "hot" domain are mathematically guaranteed to
                                # exceed the crawl-task watchdog before their turn
                                # ever comes. Confirmed live: a pilot run against
                                # thebalochistanpost.net alone produced 324 distinct,
                                # real (non-duplicate) candidate URLs this way,
                                #99.5% of which then failed on the watchdog. Cap
                                # each domain's TOTAL contribution across the whole
                                # job (not per-keyword) via a shared counter.
                                domain = sr_future_domain.get(fut)
                                if (domain and domain_candidate_budget is not None
                                        and domain_budget_lock is not None):
                                    with domain_budget_lock:
                                        used = domain_candidate_budget.get(domain, 0)
                                        remaining = max(0, _MAX_CANDIDATES_PER_DOMAIN - used)
                                        if remaining < len(results):
                                            results = results[:remaining]
                                        domain_candidate_budget[domain] = used + len(results)
                                candidate_urls.extend(results)
                            except Exception:
                                pass
                finally:
                    wait_on_shutdown = not is_job_stopped(search_id)
                    sr_pool.shutdown(wait=wait_on_shutdown)

            candidate_urls = list(dict.fromkeys(candidate_urls))
            candidate_urls = filter_candidate_urls(candidate_urls)

            # Drop candidates with a KNOWN stale published_at before a fetch is ever
            # attempted - this is what recovers the 45s-watchdog-timeout loss. URLs
            # with no known date (most legacy-path URLs) are never dropped here, per
            # the discovery layer's never-fabricate/never-assume-absence-is-stale
            # contract.
            if url_metadata:
                _effective_since = query.date_range_start
                if _effective_since is None:
                    from datetime import timedelta as _timedelta2
                    _effective_since = datetime.now(timezone.utc) - _timedelta2(days=90)
                if _effective_since.tzinfo is None:
                    _effective_since = _effective_since.replace(tzinfo=timezone.utc)
                _before = len(candidate_urls)
                candidate_urls = [
                    u for u in candidate_urls
                    if not (u in url_metadata and url_metadata[u].published_at
                            and url_metadata[u].published_at < _effective_since)
                ]
                _dropped = _before - len(candidate_urls)
                if _dropped:
                    print(f"[Discovery] Dropped {_dropped} stale URL(s) before fetch for '{kw}' "
                          f"(published_at < {_effective_since.isoformat()})")

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

        # Candidate cap - fair, round-robin across domains (Fix B, see
        # _fair_trim_candidates_across_domains's docstring for why a naive
        # candidate_urls[:_max_candidates] silently starved most domains at
        # 61-source scale).
        import os as _os3
        _max_candidates = int(_os3.environ.get("KS_MAX_CANDIDATE_URLS", "500"))
        if len(candidate_urls) > _max_candidates:
            print(f"[Cap] Trimming {len(candidate_urls)} to {_max_candidates} for '{kw}' "
                  f"(fair, round-robin across domains)")
            candidate_urls = _fair_trim_candidates_across_domains(candidate_urls, _max_candidates, get_domain)

        if not candidate_urls:
            if not crawl_only and mark_completed:
                if kp_record:
                    kp_record.status = "completed"
                    kp_record.articles_found = 0
                    kp_record.completed_at = datetime.now(timezone.utc)
                    db.commit()
            return []

        # ── Step 2: Initialize DB records — bulk approach ───────────────────────────
        db_urls = []
        if crawl_only:
            if candidate_urls:
                # Retrieve from database
                db_urls = db.scalars(select(CrawledURL).where(
                    CrawledURL.search_id == search_id,
                    CrawledURL.url.in_(candidate_urls)
                )).all()
        else:
            # Single query to find already-existing URLs for this search_id
            existing_records = {
                r.url: r for r in db.scalars(select(CrawledURL).where(
                    CrawledURL.search_id == search_id
                )).all()
            }

            new_records = []
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
                    _du = url_metadata.get(url)
                    new_rec = CrawledURL(
                        search_id=search_id,
                        url=url,
                        domain=domain_val,
                        status="pending",
                        title=(_du.title if _du else None),
                        relevance_score=(_du.relevance_score if _du and _du.relevance_score is not None else 0.0),
                        discovered_at=(_du.published_at if _du and _du.published_at else datetime.now(timezone.utc)),
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

        if discover_only:
            return candidate_urls

        # ── Step 3: Crawl pool ──────────────────────────────────────────────────────
        # Filter URLs to crawl based on Chinese classification phase
        db_urls_to_crawl = []
        for db_url in db_urls:
            is_chinese = is_chinese_url(db_url.url)
            if chinese_only is not None:
                if chinese_only and not is_chinese:
                    continue
                if not chinese_only and is_chinese:
                    continue
            db_urls_to_crawl.append(db_url)

        if not db_urls_to_crawl:
            return candidate_urls

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

        def _domain_crawl_delay(domain: str) -> Optional[float]:
            # config/site_profiles.json declares per-site politeness requirements
            # (e.g. irrawaddy.com 10s, dvb.no 2s) that are verified load-bearing for
            # avoiding an IP block - honour them at fetch time too, not just during
            # discovery. Falls through to crawl_url_task's own KS_DOMAIN_RATE_LIMIT
            # global default (1.0s) for any domain with no profile or no declared
            # crawl_delay, exactly like base.DiscoveryStrategy.crawl_delay() does.
            #
            # Profiles are keyed by the site's registered domain, but several are
            # scoped to a subdomain the actual crawled URLs live on (e.g.
            # irrawaddy.com -> burma.irrawaddy.com, cgtn.com -> newsaf.cgtn.com,
            # dvb.no -> burmese.dvb.no/english.dvb.no) - db_url.domain will be that
            # subdomain, not the bare profile key, so match by suffix, same as
            # base.DiscoveryStrategy.host_allowed()'s default scoping.
            profiles = _load_site_profiles()
            profile = profiles.get(domain)
            if profile is None:
                for prof_domain, prof in profiles.items():
                    if domain == prof_domain or domain.endswith("." + prof_domain):
                        profile = prof
                        break
            if not profile:
                return None
            d = (profile.get("robots") or {}).get("crawl_delay")
            try:
                return float(d) if d is not None else None
            except (TypeError, ValueError):
                return None

        try:
            for db_url in db_urls_to_crawl:
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
                    domain_rate_limit=_domain_crawl_delay(db_url.domain),
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

                # Watchdog check for tasks running too long. Raised from the
                # original 45s: that value measures wall-clock time since a task
                # was SUBMITTED to the executor, not since it started actually
                # fetching - and per-domain crawl-fetch rate limiting is shared/
                # serialized (~1 req/sec by default, or a site's declared
                # crawl_delay). A domain with N queued candidates has its LAST
                # task waiting ~N seconds just for its rate-limit turn before
                # fetching anything; with the per-domain candidate budget above
                # (_MAX_CANDIDATES_PER_DOMAIN, default 100) that worst case is
                # ~100s, so 120s leaves real margin for genuinely slow fetches on
                # top of that queueing delay rather than killing tasks that were
                # simply waiting their turn.
                _CRAWL_WATCHDOG_S = float(_os_qm.environ.get("KS_CRAWL_WATCHDOG_S", "120"))
                now = time.time()
                still_pending = []
                for future in pending:
                    info = futures[future]
                    elapsed = now - info["start_time"]
                    if elapsed > _CRAWL_WATCHDOG_S:
                        has_abandoned = True
                        url_id = info["url_id"]
                        future.cancel()  # Try to cancel
                        print(f"[Watchdog] Crawl task for URL ID {url_id} timed out "
                              f"(>{_CRAWL_WATCHDOG_S:.0f}s) and was abandoned.")

                        db_item = db.scalars(select(CrawledURL).where(CrawledURL.id == url_id)).first()
                        if db_item and db_item.status == "pending":
                            db_item.status = "failed"
                            db_item.error_message = f"Task exceeded maximum duration of {_CRAWL_WATCHDOG_S:.0f} seconds."
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
        if mark_completed or is_job_stopped(search_id):
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
                # The exception that landed us here may have left this Session's
                # transaction in a failed/aborted state (e.g. a
                # "concurrent operations are not permitted" or other DB error) -
                # roll it back first so the failure write below isn't itself
                # rejected because of a stale transaction.
                db.rollback()
                kp_record.status = "failed"
                kp_record.completed_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as mark_failed_err:
                print(f"[ERROR] Failed to record '{kw}' as failed after the error above: {mark_failed_err}")
    finally:
        db.close()

    return candidate_urls

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

    # Thread-safe, read-only snapshot handed to every discovery/keyword worker
    # thread from here on instead of the live `query` ORM object - see
    # _snapshot_query()'s docstring for why sharing the live object across
    # threads causes concurrent-session errors under real DB load.
    query_ro = _snapshot_query(query)

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

        # Shared, cross-keyword, cross-thread cap on how many candidates a single
        # domain can contribute - across the WHOLE job - via any discovery
        # mechanism (site-restricted web search, SiteSearchDetector, AND primary
        # per-site discovery below). See _MAX_CANDIDATES_PER_DOMAIN's definition
        # for why this exists. Created here (before the pre-discovery call) so
        # run_direct_discovery()'s keyword-independent pre-discovery pass draws
        # from the same shared budget as every per-keyword call later in this job.
        domain_candidate_budget: Dict[str, int] = {}
        domain_budget_lock = threading.Lock()

        # Pre-discover keyword-independent candidate URLs if source_type == "direct" or sitemap/feed
        pre_discovered_candidates = None
        pre_discovered_domains = None

        if query.source_type == "direct":
            raw_direct_urls = [line.strip() for line in (query.direct_urls or "").split("\n") if line.strip()]
            unique_raw_urls = list(dict.fromkeys(raw_direct_urls))
            print(f"[Direct Discovery] Running keyword-independent discovery for {len(unique_raw_urls)} unique URLs...")
            pre_discovered_candidates, pre_discovered_domains = run_direct_discovery(
                unique_raw_urls, query_ro,
                domain_candidate_budget=domain_candidate_budget,
                domain_budget_lock=domain_budget_lock,
            )
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
        # domain_candidate_budget/domain_budget_lock were already created above,
        # before the pre-discovery call, so run_direct_discovery()'s
        # keyword-independent pass shares the same budget as every per-keyword
        # call below.
        import os
        _kw_max_workers = min(len(keywords_list), int(
            os.environ.get("KS_MAX_KEYWORD_WORKERS", "8")
        ))

        # 1. DISCOVERY PHASE
        # Concurrently discover all candidate URLs across all keywords.
        keyword_candidates = {}
        kw_pool = ThreadPoolExecutor(max_workers=_kw_max_workers, thread_name_prefix="KWDiscover")
        try:
            kw_futures = {
                kw_pool.submit(
                    _process_single_keyword,
                    search_id=search_id,
                    kw=kw,
                    query=query_ro,
                    languages_filter=languages_filter,
                    seen_content_hashes=seen_content_hashes,
                    seen_simhashes=seen_simhashes,
                    seen_lock=seen_lock,
                    total_keyword_count=len(keywords_list),
                    pre_discovered_candidates=pre_discovered_candidates,
                    pre_discovered_domains=pre_discovered_domains,
                    discover_only=True,
                    domain_candidate_budget=domain_candidate_budget,
                    domain_budget_lock=domain_budget_lock,
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
                    candidates = fut.result()
                    keyword_candidates[kw_label] = candidates
                except Exception as kw_err:
                    print(f"[ERROR] Discovery worker '{kw_label}' raised: {kw_err}")
                    keyword_candidates[kw_label] = []
        finally:
            wait_on_shutdown = not (is_job_stopped(search_id) or _queue_stop_event.is_set())
            kw_pool.shutdown(wait=wait_on_shutdown)

        # Classify candidate URLs to see if we have Chinese or normal URLs
        has_chinese = False
        has_normal = False
        
        for kw_label, cand_list in keyword_candidates.items():
            for url in cand_list:
                if is_chinese_url(url):
                    has_chinese = True
                else:
                    has_normal = True

        # Check if VPN is disabled via environment configuration
        import os as _os_env
        disable_vpn = _os_env.environ.get("KS_DISABLE_VPN", "false").lower() == "true"

        # Phase flags
        run_vpn_phase = has_chinese and not disable_vpn and not (_queue_stop_event.is_set() or is_job_stopped(search_id))
        run_normal_phase = (has_normal or (has_chinese and disable_vpn)) and not (_queue_stop_event.is_set() or is_job_stopped(search_id))

        import backend.expressvpn_router as evpn

        # Phase 1: VPN Phase
        if run_vpn_phase:
            query.status_message = "Connecting to Singapore VPN..."
            db.commit()
            
            try:
                with evpn.VPNLockContext() as lock:
                    evpn.connect_singapore()
                    evpn.verify_singapore_ip()
                    
                    query.status_message = "Crawling Chinese sources via Singapore"
                    db.commit()

                    # Run crawls for Chinese URLs in parallel
                    kw_pool = ThreadPoolExecutor(max_workers=_kw_max_workers, thread_name_prefix="KWVPN")
                    try:
                        mark_comp = not run_normal_phase
                        kw_futures = {
                            kw_pool.submit(
                                _process_single_keyword,
                                search_id=search_id,
                                kw=kw,
                                query=query_ro,
                                languages_filter=languages_filter,
                                seen_content_hashes=seen_content_hashes,
                                seen_simhashes=seen_simhashes,
                                seen_lock=seen_lock,
                                total_keyword_count=len(keywords_list),
                                pre_discovered_candidates=pre_discovered_candidates,
                                pre_discovered_domains=pre_discovered_domains,
                                crawl_only=True,
                                chinese_only=True,
                                discovered_urls=keyword_candidates.get(kw),
                                mark_completed=mark_comp,
                                domain_candidate_budget=domain_candidate_budget,
                                domain_budget_lock=domain_budget_lock,
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
                                print(f"[ERROR] VPN crawl worker '{kw_label}' raised: {kw_err}")
                    finally:
                        wait_on_shutdown = not (is_job_stopped(search_id) or _queue_stop_event.is_set())
                        kw_pool.shutdown(wait=wait_on_shutdown)
                    
                    query.status_message = "Disconnecting VPN..."
                    db.commit()
                    
                    evpn.disconnect()
                    evpn.verify_normal_ip()
                    
            except Exception as vpn_err:
                print(f"[ERROR] VPN phase failed: {vpn_err}")
                # Gather all pending Chinese URLs and mark them as failed with reason
                try:
                    db_fresh = SessionLocal()
                    chinese_urls = db_fresh.scalars(select(CrawledURL).where(
                        CrawledURL.search_id == search_id,
                        CrawledURL.status == "pending"
                    )).all()
                    
                    failed_count = 0
                    for c_url in chinese_urls:
                        if is_chinese_url(c_url.url):
                            c_url.status = "failed"
                            c_url.error_message = f"VPN routing failure: {vpn_err}"
                            failed_count += 1
                            
                    if failed_count > 0:
                        db_fresh.execute(
                            update(SearchQuery)
                            .where(SearchQuery.id == search_id)
                            .values(
                                total_urls_crawled=SearchQuery.total_urls_crawled + failed_count
                            )
                        )
                        db_fresh.commit()
                    db_fresh.close()
                except Exception as db_err:
                    print(f"[ERROR] Failed to mark Chinese URLs as failed: {db_err}")

        # Phase 2: Normal Phase
        if run_normal_phase:
            query.status_message = "Crawling remaining sources"
            db.commit()

            kw_pool = ThreadPoolExecutor(max_workers=_kw_max_workers, thread_name_prefix="KWNormal")
            try:
                kw_futures = {
                    kw_pool.submit(
                        _process_single_keyword,
                        search_id=search_id,
                        kw=kw,
                        query=query_ro,
                        languages_filter=languages_filter,
                        seen_content_hashes=seen_content_hashes,
                        seen_simhashes=seen_simhashes,
                        seen_lock=seen_lock,
                        total_keyword_count=len(keywords_list),
                        pre_discovered_candidates=pre_discovered_candidates,
                        pre_discovered_domains=pre_discovered_domains,
                        crawl_only=True,
                        chinese_only=None if disable_vpn else False,
                        discovered_urls=keyword_candidates.get(kw),
                        mark_completed=True,
                        domain_candidate_budget=domain_candidate_budget,
                        domain_budget_lock=domain_budget_lock,
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
                        print(f"[ERROR] Normal crawl worker '{kw_label}' raised: {kw_err}")
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
            query.status_message = None
            query.updated_at = datetime.now(timezone.utc)
            db.commit()
        elif not _queue_stop_event.is_set():
            query.status = "completed"
            query.status_message = None
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
        query.status_message = None
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
