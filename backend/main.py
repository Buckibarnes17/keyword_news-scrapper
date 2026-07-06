# ## Changes
# - Added timedelta and timezone to datetime imports.
# - Replaced deprecated @app.on_event with lifespan context manager.
# - Replaced datetime.utcnow() with datetime.now(timezone.utc).
# - Implemented Bearer token verification API authentication via API_TOKEN environment variable.
# - Implemented slowapi endpoint rate limiting (10/min on POST/DELETE, 30/min on GET).
# - Added stuck-job recovery recover_stuck_jobs() on server startup.

import os
import json
import uuid
import logging
import logging.config

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
})
logger = logging.getLogger("keywordscout.main")
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict
from fastapi import FastAPI, Depends, HTTPException, Query, Response, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager

from backend.database import Base, engine, get_db, init_db, SessionLocal
from backend.models import SearchQuery, CrawledURL, SearchSchedule, KeywordProgress
from backend.schemas import (
    SearchQueryCreate, SearchQueryResponse, 
    PaginatedCrawledURLResponse, CrawledURLResponse,
    SearchScheduleCreate, SearchScheduleResponse,
    ScrapeRequest, FirecrawlResponse
)
from backend.queue_manager import start_queue_worker, stop_queue_worker, request_job_stop
from backend.scheduler import start_scheduler, stop_scheduler, calculate_next_run
from backend.exporter import export_results

# Create and migrate database tables on startup
init_db()
try:
    from backend.postgres_integration import init_postgres_db
    init_postgres_db(verbose=True)
except Exception as pg_init_err:
    print(f"[PostgreSQL Warning] Could not initialize database on startup: {pg_init_err}")

# Bearer Token Auth Logic
API_TOKEN = os.environ.get("API_TOKEN", "changeme")
security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token.")
    return credentials.credentials

# Stuck Job Recovery Logic
def recover_stuck_jobs():
    db = SessionLocal()
    try:
        stuck = db.query(SearchQuery).filter(SearchQuery.status == "processing").all()
        for job in stuck:
            job.status = "pending"
            job.error_message = "Recovered after server restart."
            job.updated_at = datetime.now(timezone.utc)
        if stuck:
            db.commit()
            print(f"[Recovery] Reset {len(stuck)} stuck jobs to pending.")
    except Exception as e:
        print(f"[Recovery Error] Failed to recover stuck jobs: {e}")
    finally:
        db.close()

# Lifespan context manager replacing startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    recover_stuck_jobs()
    start_queue_worker()
    start_scheduler()
    print("Application services initialized.")
    yield
    stop_queue_worker()
    stop_scheduler()
    print("Application services shut down.")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Keyword Scraper & Crawler API",
    description="Asynchronously crawls and scrapes the web for target keywords.",
    version="1.0.0",
    lifespan=lifespan
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Request-ID middleware: stamps every response with X-Request-ID for traceability
from starlette.middleware.base import BaseHTTPMiddleware

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

app.add_middleware(RequestIDMiddleware)

# Enable CORS for development flexibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {type(exc).__name__}: {str(exc)}"}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("Validation error on %s: %s", request.url.path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# REST APIs

@app.post("/api/search", response_model=SearchQueryResponse)
@limiter.limit("10/minute")
def create_search(request: Request, payload: SearchQueryCreate, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """
    Submits a search crawl request.
    Can be a web search or a custom list of raw URLs/sitemaps.
    """
    domains_str = json.dumps(payload.domains_filter) if payload.domains_filter else None
    languages_str = json.dumps(payload.languages_filter) if payload.languages_filter else None

    # Handle direct input checks and validation
    kw_clean = (payload.keyword or "").strip()
    
    if payload.source_type == "search" and not kw_clean:
        raise HTTPException(status_code=400, detail="keyword is required when source_type is 'search'")
        
    if payload.source_type == "direct":
        if not payload.direct_urls or not payload.direct_urls.strip():
            raise HTTPException(status_code=400, detail="direct_urls field is required when source_type is 'direct'")

    new_query = SearchQuery(
        keyword=kw_clean,
        match_type=payload.match_type,
        case_sensitive=payload.case_sensitive,
        exact_match=payload.exact_match,
        domains_filter=domains_str,
        languages_filter=languages_str,
        date_range_start=payload.date_range_start,
        date_range_end=payload.date_range_end,
        engine=payload.engine,
        source_type=payload.source_type,
        direct_urls=payload.direct_urls,
        ignore_robots=payload.ignore_robots,
        proxy_url=payload.proxy_url,          # NEW
        status="pending"
    )
    
    db.add(new_query)
    db.commit()
    db.refresh(new_query)
    return new_query

@app.get("/api/tor/status")
def get_tor_status():
    """
    Checks whether the local Tor SOCKS5 proxy is reachable on 127.0.0.1:9050.
    Called by the frontend toggle pre-flight check before launching a Tor job.

    Returns:
        200 always — the JSON body contains the reachability result.
        {"reachable": true,  "message": "...", "proxy_url": "socks5h://127.0.0.1:9050"}
        {"reachable": false, "message": "Tor SOCKS5 proxy is NOT running ...", "proxy_url": null}
    """
    from backend.tor_router import check_tor_reachability, TOR_SOCKS5_URL
    result = check_tor_reachability()
    return {
        **result,
        "proxy_url": TOR_SOCKS5_URL if result["reachable"] else None,
        "tor_port": 9050,
        "tor_host": "127.0.0.1"
    }

@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    """Returns system health: DB connectivity and worker status."""
    from backend.queue_manager import _worker_thread
    from sqlalchemy import text
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.error("DB health check failed: %s", e)
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "disconnected",
        "queue_worker": "running" if (_worker_thread and _worker_thread.is_alive()) else "stopped",
        "version": "2.0"
    }

@app.get("/api/history", response_model=List[SearchQueryResponse])
@limiter.limit("30/minute")
def get_search_history(request: Request, db: Session = Depends(get_db)):
    """Returns all historical search query records."""
    queries = db.query(SearchQuery).order_by(SearchQuery.created_at.desc()).all()
    return queries

@app.delete("/api/search/{search_id}")
@limiter.limit("10/minute")
def delete_search(request: Request, search_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """Deletes a search query record and all its crawled URLs."""
    query = db.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    if not query:
        raise HTTPException(status_code=404, detail="Search query not found")
    db.delete(query)
    db.commit()
    return {"message": f"Search run {search_id} successfully deleted"}

@app.post("/api/search/{search_id}/stop")
@limiter.limit("10/minute")
def stop_search(request: Request, search_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """Gracefully aborts a search query run."""
    query = db.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    if not query:
        raise HTTPException(status_code=404, detail="Search query not found")
        
    if query.status in ("completed", "failed", "aborted"):
        return {"message": f"Search run {search_id} is already in {query.status} state."}

    request_job_stop(search_id)

    # Set status to aborted immediately in the DB to reflect in UI instantly
    query.status = "aborted"
    query.updated_at = datetime.now(timezone.utc)
    db.commit()
        
    return {"message": f"Stop signal sent to search run {search_id}."}

@app.post("/api/search/{search_id}/retry", response_model=SearchQueryResponse)
@limiter.limit("10/minute")
def retry_search(request: Request, search_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """Duplicates a past search run config and creates a new pending run."""
    old_query = db.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    if not old_query:
        raise HTTPException(status_code=404, detail="Search query not found")
        
    new_query = SearchQuery(
        keyword=old_query.keyword,
        match_type=old_query.match_type,
        case_sensitive=old_query.case_sensitive,
        exact_match=old_query.exact_match,
        domains_filter=old_query.domains_filter,
        languages_filter=old_query.languages_filter,
        date_range_start=old_query.date_range_start,
        date_range_end=old_query.date_range_end,
        engine=old_query.engine,
        source_type=old_query.source_type,
        direct_urls=old_query.direct_urls,
        ignore_robots=old_query.ignore_robots,
        status="pending"
    )
    db.add(new_query)
    db.commit()
    db.refresh(new_query)
    return new_query

def _serialize_crawled_url(url_obj) -> dict:
    """Converts a CrawledURL SQLAlchemy model to a plain, fully serializable dict.
    This guarantees the 'status' and all other fields are always present in the response.
    """
    return {
        "id": url_obj.id,
        "search_id": url_obj.search_id,
        "url": url_obj.url,
        "domain": url_obj.domain,
        "title": url_obj.title or "",
        "snippet": url_obj.snippet or "",
        "occurrences": url_obj.occurrences or 0,
        "found_in_title": bool(url_obj.found_in_title),
        "found_in_description": bool(url_obj.found_in_description),
        "found_in_body": bool(url_obj.found_in_body),
        "found_in_url": bool(url_obj.found_in_url),
        "language": url_obj.language,
        "status": url_obj.status or "pending",
        "error_message": url_obj.error_message,
        "relevance_score": float(url_obj.relevance_score or 0.0),
        "is_duplicate": bool(url_obj.is_duplicate),
        "description": url_obj.description or "",
        "full_content": url_obj.full_content or "",
        "raw_html": url_obj.raw_html or "",
        "author": url_obj.author or "Unknown",
        "image_url": url_obj.image_url,
        "discovered_at": url_obj.discovered_at.isoformat() if url_obj.discovered_at else None,
        "matched_keywords": url_obj.matched_keywords or "[]",
    }


@app.get("/api/results/{search_id}")
@limiter.limit("300/minute")
def get_search_results(
    request: Request,
    search_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1),

    domain_query: Optional[str] = Query(None),
    min_relevance: Optional[float] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Returns search query status metadata and a paginated, filterable list of crawled URLs.
    """
    query_record = db.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    if not query_record:
        raise HTTPException(status_code=404, detail="Search query not found")

    # Start building base query for CrawledURL
    crawled_base_query = db.query(CrawledURL).filter(
        CrawledURL.search_id == search_id
    )

    # Apply filters
    if domain_query:
        crawled_base_query = crawled_base_query.filter(CrawledURL.domain.contains(domain_query.lower()))
    if min_relevance is not None:
        crawled_base_query = crawled_base_query.filter(CrawledURL.relevance_score >= min_relevance)

    # Count total matched before paginating
    total_count = crawled_base_query.count()

    # Paginated results (order by relevance score desc, with error items at bottom)
    results = crawled_base_query.order_by(
        CrawledURL.relevance_score.desc(),
        CrawledURL.discovered_at.desc()
    ).offset((page - 1) * limit).limit(limit).all()

    # Form response dict containing query meta and list of crawled items
    return {
        "search_meta": {
            "id": query_record.id,
            "keyword": query_record.keyword,
            "match_type": query_record.match_type,
            "case_sensitive": query_record.case_sensitive,
            "exact_match": query_record.exact_match,
            "engine": query_record.engine,
            "source_type": query_record.source_type,
            "ignore_robots": query_record.ignore_robots,
            "status": query_record.status,
            "total_urls_found": query_record.total_urls_found,
            "total_urls_crawled": query_record.total_urls_crawled,
            "total_urls_matched": query_record.total_urls_matched,
            "error_message": query_record.error_message,
            "created_at": query_record.created_at,
            "updated_at": query_record.updated_at
        },
        "results": {
            "total": total_count,
            "page": page,
            "limit": limit,
            "items": [_serialize_crawled_url(r) for r in results]
        }
    }

@app.get("/api/export/{search_id}")
@limiter.limit("30/minute")
def export_search(
    request: Request,
    search_id: int,
    format: str = Query("csv"),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    exclude_duplicates: bool = Query(True),
    sort_by: Optional[str] = Query(None),
    sort_desc: bool = Query(True),
    db: Session = Depends(get_db),
    token: str = Depends(verify_token)
):
    """
    Generates and streams an export file (CSV, XLSX, JSON, Parquet) for a search.
    """
    try:
        data_bytes, media_type = export_results(
            search_id,
            format,
            db,
            q=q,
            status=status,
            exclude_duplicates=exclude_duplicates,
            sort_by=sort_by,
            sort_desc=sort_desc
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

    query_record = db.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    keyword_clean = "".join(c for c in query_record.keyword if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    filename = f"keyword_results_{search_id}_{keyword_clean}.{format}"
    
    headers = {
        "Content-Disposition": f"attachment; filename={filename}"
    }

    return Response(content=data_bytes, media_type=media_type, headers=headers)

@app.post("/api/export/{search_id}/postgres")
@limiter.limit("10/minute")
def export_search_postgres(request: Request, search_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """
    Exports crawling results for a query into the configured PostgreSQL database.
    """
    from backend.postgres_integration import export_search_to_postgres
    try:
        inserted, updated = export_search_to_postgres(search_id, db)
        return {
            "status": "success",
            "message": f"Successfully integrated data. Inserted {inserted} and updated {updated} records in the PostgreSQL database."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PostgreSQL integration failed: {str(e)}")

@app.post("/api/schedules", response_model=SearchScheduleResponse)
@limiter.limit("10/minute")
def create_schedule(request: Request, payload: SearchScheduleCreate, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """Creates a new recurring keyword search schedule."""
    # Clean keyword
    kw_clean = (payload.keyword or "").strip()
    payload.keyword = kw_clean
    payload.config.keyword = (payload.config.keyword or "").strip()

    # Validate config mode parameters
    if payload.config.source_type == "search" and not payload.config.keyword:
        raise HTTPException(status_code=400, detail="keyword is required for web search schedule mode")
        
    if payload.config.source_type == "direct":
        if not payload.config.direct_urls or not payload.config.direct_urls.strip():
            raise HTTPException(status_code=400, detail="direct_urls field is required for direct URL schedule mode")

    # Check frequency
    if payload.frequency not in ("daily", "weekly", "monthly"):
        raise HTTPException(status_code=400, detail="Frequency must be one of: daily, weekly, monthly")

    # Set initial next_run time
    if payload.next_run:
        next_run = payload.next_run
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
    else:
        # Fallback to backend calculation based on the config
        config_dict = payload.config.dict()
        next_run = calculate_next_run(payload.frequency, config_dict, datetime.now(timezone.utc))

    new_sched = SearchSchedule(
        keyword=kw_clean,
        frequency=payload.frequency,
        active=True,
        engine=payload.engine or "fast",
        config_json=payload.config.json(),
        next_run=next_run
    )

    db.add(new_sched)
    db.commit()
    db.refresh(new_sched)
    return new_sched

@app.get("/api/schedules")
@limiter.limit("30/minute")
def list_schedules(request: Request, db: Session = Depends(get_db)):
    """Lists all active and inactive schedules."""
    schedules = db.query(SearchSchedule).order_by(SearchSchedule.created_at.desc()).all()
    return schedules

@app.delete("/api/schedules/{schedule_id}")
@limiter.limit("10/minute")
def delete_schedule(request: Request, schedule_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    """Deletes a schedule."""
    sched = db.query(SearchSchedule).filter(SearchSchedule.id == schedule_id).first()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(sched)
    db.commit()
    return {"message": f"Schedule {schedule_id} successfully deleted"}

@app.post("/api/scrape", response_model=FirecrawlResponse)
@limiter.limit("10/minute")
def scrape_url(request: Request, payload: ScrapeRequest, token: str = Depends(verify_token)):
    """
    Live scrapes any target URL and formats the response according to the Firecrawl schema.
    """
    from backend.crawler import Crawler
    from backend.firecrawl_converter import convert_html_to_firecrawl_schema

    crawler = Crawler(proxy_url=payload.proxy_url)
    try:
        html_content = crawler.fetch_page(
            payload.url,
            engine=payload.engine or "fast",
            ignore_robots=payload.ignore_robots or False
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch page: {str(e)}")
    finally:
        crawler.close()

    try:
        firecrawl_data = convert_html_to_firecrawl_schema(html_content, payload.url)
        return firecrawl_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to convert page content: {str(e)}")

@app.get("/api/results/crawled/{url_id}/firecrawl", response_model=FirecrawlResponse)
@limiter.limit("30/minute")
def get_crawled_url_as_firecrawl(request: Request, url_id: int, db: Session = Depends(get_db)):
    """
    Retrieves a crawled URL record from the database and returns it structured
    according to the Firecrawl response schema.
    """
    crawled_url = db.query(CrawledURL).filter(CrawledURL.id == url_id).first()
    if not crawled_url:
        raise HTTPException(status_code=404, detail="Crawled URL not found")

    return {
        "success": True,
        "data": {
            "markdown": crawled_url.full_content or "",
            "html": crawled_url.raw_html or "",
            "metadata": {
                "title": crawled_url.title or "Untitled",
                "description": crawled_url.description or "",
                "language": crawled_url.language or "en",
                "sourceURL": crawled_url.url,
                "statusCode": 200
            },
            "links": [],
            "images": [{"src": crawled_url.image_url, "alt": "lead image", "caption": "", "width": 0, "height": 0}] if crawled_url.image_url else [],
            "videos": [],
            "content": {
                "headings": [],
                "paragraphs": [crawled_url.snippet] if crawled_url.snippet else [],
                "lists": [],
                "tables": [],
                "codeBlocks": [],
                "quotes": []
            }
        }
    }

# Mount Static Files (serves the frontend files)

# Ensure the static folder exists
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
os.makedirs(static_dir, exist_ok=True)

# Mount index.html at root, fallback to files
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
