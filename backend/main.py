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
from fastapi import FastAPI, Depends, HTTPException, Query, Response, Request, Security, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import select, update, delete, func

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager

from backend.database import Base, engine, init_db, SessionLocal, get_db
from backend.models import SearchQuery, CrawledURL, SearchSchedule, KeywordProgress, User
from backend.schemas import (
    SearchQueryCreate, SearchQueryResponse, 
    PaginatedCrawledURLResponse, CrawledURLResponse,
    SearchScheduleCreate, SearchScheduleResponse,
    ScrapeRequest, FirecrawlResponse
)
from backend.queue_manager import start_queue_worker, stop_queue_worker, request_job_stop
from backend.scheduler import start_scheduler, stop_scheduler, calculate_next_run
from backend.exporter import export_results

def _get_config_path(filename: str) -> str:
    """Returns the absolute path to a file inside config/ at the project root."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "config", filename)

def _read_config_file(filename: str) -> dict:
    """Reads and parses a JSON file from config/. Raises HTTPException on error."""
    import json as _json
    path = _get_config_path(filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
            detail=f"Config file not found: config/{filename}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        raise HTTPException(status_code=422,
            detail=f"Failed to read config/{filename}: {e}")

def _write_config_file(filename: str, data: dict) -> None:
    """Writes a dict to a JSON file in config/ atomically. Raises HTTPException on error."""
    import json as _json
    import tempfile
    path = _get_config_path(filename)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        dir_name = os.path.dirname(path)
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8", suffix=".tmp") as f:
            _json.dump(data, f, indent=2, ensure_ascii=False)
            temp_path = f.name
        os.replace(temp_path, path)
    except Exception as e:
        if 'temp_path' in locals() and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise HTTPException(status_code=500,
            detail=f"Failed to write config/{filename}: {e}")

def initialize_app_state():
    """Initializes app state: database tables, postgres db connection, and config files."""
    # Ensure config/keywords.json exists on startup atomically
    _kw_config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "keywords.json"
    )
    if not os.path.exists(_kw_config_path):
        import json as _json
        import tempfile
        os.makedirs(os.path.dirname(_kw_config_path), exist_ok=True)
        _default_kw = {
            "version": "1.0",
            "description": "KeywordScout keyword config — add keywords via the Config Manager page",
            "keywords": [],
            "groups": {
                "china": "China-related keywords",
                "myanmar": "Myanmar-related keywords",
                "northeast_india": "Northeast India keywords",
                "pakistan_central_asia": "Pakistan and Central Asia keywords",
                "general": "General monitoring keywords"
            }
        }
        dir_name = os.path.dirname(_kw_config_path)
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8", suffix=".tmp") as _f:
                _json.dump(_default_kw, _f, indent=2, ensure_ascii=False)
                temp_path = _f.name
            os.replace(temp_path, _kw_config_path)
            logger.info("[Config] Created default config/keywords.json")
        except Exception as e:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
            raise e

    # Initialize databases
    init_db()
    try:
        from backend.postgres_integration import init_postgres_db
        init_postgres_db(verbose=True)
    except Exception as pg_init_err:
        print(f"[PostgreSQL Warning] Could not initialize database on startup: {pg_init_err}")

# JWT Auth Logic & Helper Imports
from backend.auth import (
    get_current_user,
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    JWT_SECRET,
    ALGORITHM
)
from backend.schemas import UserCreate, UserResponse, TokenResponse, RefreshTokenRequest
from jose import jwt, JWTError

# Stuck Job Recovery Logic
def recover_stuck_jobs():
    db = SessionLocal()
    try:
        stuck = db.scalars(select(SearchQuery).where(SearchQuery.status == "processing")).all()
        for job in stuck:
            job.status = "failed"
            job.error_message = "Recovered after server restart."
            job.updated_at = datetime.now(timezone.utc)
        if stuck:
            db.commit()
            print(f"[Recovery] Reset {len(stuck)} stuck jobs to failed.")
    except Exception as e:
        print(f"[Recovery Error] Failed to recover stuck jobs: {e}")
    finally:
        db.close()

# Lifespan context manager replacing startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_app_state()
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
    db = request.state.__dict__.get("db")
    if db:
        try:
            db.close()
        except Exception:
            pass
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {type(exc).__name__}: {str(exc)}"}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("Validation error on %s: %s", request.url.path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# REST APIs

@app.post("/api/auth/signup", response_model=UserResponse)
def signup(payload: UserCreate, db: Session = Depends(get_db)):
    # Check if user already exists
    existing_user = db.scalars(select(User).where(User.email == payload.email)).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered")
    
    hashed_pwd = get_password_hash(payload.password)
    new_user = User(email=payload.email, hashed_password=hashed_pwd)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@app.post("/api/auth/login", response_model=TokenResponse)
def login(payload: UserCreate, db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.email == payload.email)).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }

@app.post("/api/auth/refresh", response_model=TokenResponse)
def refresh(payload: RefreshTokenRequest, db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate refresh credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        token_payload = jwt.decode(payload.refresh_token, JWT_SECRET, algorithms=[ALGORITHM])
        email: str = token_payload.get("sub")
        token_type: str = token_payload.get("type")
        if email is None or token_type != "refresh":
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user = db.scalars(select(User).where(User.email == email)).first()
    if not user:
        raise credentials_exception
        
    access_token = create_access_token(data={"sub": user.email})
    new_refresh_token = create_refresh_token(data={"sub": user.email})
    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer"
    }

@app.get("/api/auth/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user

@app.post("/api/auth/logout")
def logout():
    return {"detail": "Successfully logged out"}

@app.post("/api/search", response_model=SearchQueryResponse)
@limiter.limit("10/minute")
def create_search(request: Request, payload: SearchQueryCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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

@app.get("/api/config/urls")
def get_config_urls():
    """
    Reads and returns the contents of config/urls.json.
    Used by the Config File tab in the New Crawl view to load available URLs.

    Returns the parsed JSON directly.
    Raises HTTP 404 if the file does not exist.
    Raises HTTP 422 if the file exists but is not valid JSON.
    """
    import json
    import os

    # Resolve path: config/urls.json relative to this file's parent's parent
    # i.e. project_root/config/urls.json
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path  = os.path.join(project_root, "config", "urls.json")

    if not os.path.exists(config_path):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail=(
                f"Config file not found at {config_path}. "
                "Create config/urls.json at the project root."
            )
        )
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except json.JSONDecodeError as e:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"config/urls.json is not valid JSON: {e}"
        )
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"Failed to read config file: {e}")

@app.post("/api/config/urls/validate")
def validate_config_urls(payload: dict):
    """
    Validates a config/urls.json payload against the expected schema.
    Returns validation result — does NOT write to disk.

    Expected shape: { "urls": [...], "groups": {...}, "defaults": {...} }
    """
    errors = []

    if "urls" not in payload or not isinstance(payload["urls"], list):
        errors.append("Missing or invalid 'urls' array.")

    if "groups" not in payload or not isinstance(payload["groups"], dict):
        errors.append("Missing or invalid 'groups' object.")

    required_url_fields = {"url", "label", "group", "type", "language"}
    for i, entry in enumerate(payload.get("urls", [])):
        missing = required_url_fields - set(entry.keys())
        if missing:
            errors.append(f"URL entry #{i+1} missing fields: {', '.join(sorted(missing))}")
        if "url" in entry and not entry["url"].startswith(("http://", "https://")):
            errors.append(f"URL entry #{i+1} has invalid URL: {entry['url']}")

    if errors:
        return {"valid": False, "errors": errors, "url_count": 0}

    return {
        "valid":     True,
        "errors":    [],
        "url_count": len(payload.get("urls", [])),
        "groups":    list(payload.get("groups", {}).keys())
    }

@app.post("/api/config/urls/upload")
async def upload_config_urls(file: UploadFile = File(...)):
    """
    Accepts an uploaded urls.json file, parses it, validates its schema,
    and returns the parsed content.

    The file is NOT written to disk — it is only parsed and returned.
    The client can then use the returned data to populate the config UI.

    Returns:
        {"valid": true,  "data": {...parsed config...}, "url_count": N}
        {"valid": false, "errors": [...], "data": null}
    """
    import json as _json

    # Enforce .json extension (browsers may send application/octet-stream)
    if file.filename and not file.filename.lower().endswith(".json"):
        return {
            "valid": False,
            "errors": [f"File must be a .json file. Got: {file.filename}"],
            "data": None,
            "url_count": 0
        }

    # Read the file content
    try:
        contents = await file.read()
        if len(contents) > 5 * 1024 * 1024:   # 5MB hard limit
            return {
                "valid": False,
                "errors": ["File too large. Maximum allowed size is 5MB."],
                "data": None,
                "url_count": 0
            }
    except Exception as e:
        return {"valid": False, "errors": [f"Failed to read file: {e}"],
                "data": None, "url_count": 0}

    # Parse JSON
    try:
        data = _json.loads(contents.decode("utf-8"))
    except (_json.JSONDecodeError, UnicodeDecodeError) as e:
        return {
            "valid": False,
            "errors": [f"Invalid JSON: {e}"],
            "data": None,
            "url_count": 0
        }

    # Validate schema
    errors = []
    if "urls" not in data or not isinstance(data["urls"], list):
        errors.append("Missing or invalid 'urls' array.")
    if "groups" not in data or not isinstance(data["groups"], dict):
        errors.append("Missing or invalid 'groups' object.")

    required_fields = {"url", "label", "group", "type", "language"}
    for i, entry in enumerate(data.get("urls", [])):
        missing = required_fields - set(entry.keys())
        if missing:
            errors.append(
                f"URL entry #{i+1} ('{entry.get('label','?')}') "
                f"missing fields: {', '.join(sorted(missing))}"
            )
        url_val = entry.get("url", "")
        if url_val and not url_val.startswith(("http://", "https://")):
            errors.append(
                f"URL entry #{i+1} has invalid URL (must start with http:// or https://): {url_val}"
            )

    if errors:
        return {"valid": False, "errors": errors, "data": None, "url_count": 0}

    return {
        "valid":     True,
        "errors":    [],
        "data":      data,
        "url_count": len(data.get("urls", [])),
        "groups":    list(data.get("groups", {}).keys())
    }


# ── Keywords config CRUD ──────────────────────────────────────────────────────

@app.get("/api/config/keywords")
def get_config_keywords():
    """
    Returns the full contents of config/keywords.json.
    Creates the file with defaults if it does not exist yet.
    """
    path = _get_config_path("keywords.json")
    if not os.path.exists(path):
        default = {
            "version": "1.0",
            "description": "KeywordScout keyword config — add keywords via the Config Manager page",
            "keywords": [],
            "groups": {
                "china": "China-related keywords",
                "myanmar": "Myanmar-related keywords",
                "northeast_india": "Northeast India keywords",
                "pakistan_central_asia": "Pakistan and Central Asia keywords",
                "general": "General monitoring keywords"
            }
        }
        _write_config_file("keywords.json", default)
        return default
    return _read_config_file("keywords.json")


@app.post("/api/config/keywords")
def add_config_keyword(entry: dict):
    """
    Appends a new keyword entry to config/keywords.json.
    Required fields: keyword (str), group (str), match_type (str), notes (str).
    Returns the updated keywords list.
    """
    required = {"keyword", "group", "match_type"}
    missing = required - set(entry.keys())
    if missing:
        raise HTTPException(status_code=422,
            detail=f"Missing required fields: {', '.join(sorted(missing))}")
    if not entry.get("keyword", "").strip():
        raise HTTPException(status_code=422, detail="keyword cannot be empty.")
    if entry.get("match_type") not in ("phrase", "boolean"):
        raise HTTPException(status_code=422,
            detail="match_type must be 'phrase' or 'boolean'.")

    data        = _read_config_file("keywords.json")
    group_key   = entry.get("group", "general").strip()
    group_label = entry.get("group_label", "").strip()

    new_entry = {
        "keyword":    entry["keyword"].strip(),
        "group":      group_key,
        "match_type": entry["match_type"],
        "notes":      entry.get("notes", "")
    }
    data.setdefault("keywords", []).append(new_entry)

    # Persist new custom group into data["groups"] if it doesn't already exist
    data.setdefault("groups", {})
    if group_key and group_key not in data["groups"]:
        data["groups"][group_key] = group_label or group_key.replace("_", " ").title()
        logger.info("[Config] New group added to keywords.json: %s = %s",
                    group_key, data["groups"][group_key])

    _write_config_file("keywords.json", data)
    return {
        "success":  True,
        "keywords": data["keywords"],
        "groups":   data["groups"]
    }


@app.post("/api/config/keywords/bulk")
def add_config_keywords_bulk(payload: dict):
    """
    Accepts list of keyword entries, validates them,
    and appends the successful ones to config/keywords.json.
    Payload: {"keywords": [ {keyword, group, match_type, notes}, ... ]}
    """
    entries = payload.get("keywords", [])
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="keywords must be a list")
        
    if len(entries) > 500:
        raise HTTPException(status_code=400, detail="Batch exceeds the limit of 500 entries.")

    data = _read_config_file("keywords.json")
    
    added = []
    skipped = []
    
    required = {"keyword", "group", "match_type"}
    
    for item in entries:
        missing = required - set(item.keys())
        if not missing:
            empty = [f for f in required if not str(item.get(f, "")).strip()]
            if empty:
                skipped.append({
                    "entry": item,
                    "reason": f"Missing required fields: {', '.join(sorted(empty))}"
                })
                continue
        else:
            skipped.append({
                "entry": item,
                "reason": f"Missing required fields: {', '.join(sorted(missing))}"
            })
            continue
            
        keyword_val = item.get("keyword", "").strip()
        if not keyword_val:
            skipped.append({
                "entry": item,
                "reason": "keyword cannot be empty."
            })
            continue
            
        match_type_val = item.get("match_type")
        if match_type_val not in ("phrase", "boolean"):
            skipped.append({
                "entry": item,
                "reason": "match_type must be 'phrase' or 'boolean'."
            })
            continue
            
        group_key = item.get("group", "general").strip()
        group_label = item.get("group_label", "").strip()
        new_entry = {
            "keyword":    keyword_val,
            "group":      group_key,
            "match_type": match_type_val,
            "notes":      item.get("notes", "")
        }
        data.setdefault("keywords", []).append(new_entry)
        added.append(new_entry)

        # Persist new custom group if it doesn't already exist
        data.setdefault("groups", {})
        if group_key and group_key not in data["groups"]:
            data["groups"][group_key] = group_label or group_key.replace("_", " ").title()
            logger.info("[Config] New group added to keywords.json (bulk): %s = %s",
                        group_key, data["groups"][group_key])
        
    if added:
        _write_config_file("keywords.json", data)
        
    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "keywords": data.get("keywords", []),
        "groups": data.get("groups", {})
    }


@app.put("/api/config/keywords/{index}")
def update_config_keyword(index: int, entry: dict):
    """
    Updates the keyword entry at position `index` in config/keywords.json.
    Returns the updated keywords list.
    """
    data = _read_config_file("keywords.json")
    keywords = data.get("keywords", [])
    if index < 0 or index >= len(keywords):
        raise HTTPException(status_code=404,
            detail=f"No keyword at index {index}.")
    if not entry.get("keyword", "").strip():
        raise HTTPException(status_code=422, detail="keyword cannot be empty.")
    group_key   = entry.get("group", keywords[index].get("group", "general")).strip()
    group_label = entry.get("group_label", "").strip()

    keywords[index] = {
        "keyword":    entry.get("keyword", keywords[index]["keyword"]).strip(),
        "group":      group_key,
        "match_type": entry.get("match_type", keywords[index].get("match_type", "phrase")),
        "notes":      entry.get("notes", keywords[index].get("notes", ""))
    }
    data["keywords"] = keywords

    # Persist new custom group if it doesn't already exist
    data.setdefault("groups", {})
    if group_key and group_key not in data["groups"]:
        data["groups"][group_key] = group_label or group_key.replace("_", " ").title()
        logger.info("[Config] New group added to keywords.json (update): %s = %s",
                    group_key, data["groups"][group_key])

    _write_config_file("keywords.json", data)
    return {
        "success":  True,
        "keywords": keywords,
        "groups":   data["groups"]
    }


@app.delete("/api/config/keywords/{index}")
def delete_config_keyword(index: int):
    """
    Deletes the keyword entry at position `index` from config/keywords.json.
    Returns the updated keywords list.
    """
    data = _read_config_file("keywords.json")
    keywords = data.get("keywords", [])
    if index < 0 or index >= len(keywords):
        raise HTTPException(status_code=404,
            detail=f"No keyword at index {index}.")
    removed = keywords.pop(index)
    data["keywords"] = keywords
    _write_config_file("keywords.json", data)
    return {"success": True, "removed": removed, "keywords": keywords}


# ── URLs config CRUD ──────────────────────────────────────────────────────────

@app.post("/api/config/urls")
def add_config_url(entry: dict):
    """
    Appends a new URL entry to config/urls.json.
    Required fields: url, label, group, type, language.
    Returns the updated urls list.
    """
    required = {"url", "label", "group", "type", "language"}
    missing = required - set(entry.keys())
    if missing:
        raise HTTPException(status_code=422,
            detail=f"Missing required fields: {', '.join(sorted(missing))}")
    url_val = entry.get("url", "").strip()
    if not url_val.startswith(("http://", "https://")):
        raise HTTPException(status_code=422,
            detail="url must start with http:// or https://")

    data = _read_config_file("urls.json")
    # Check for duplicate URL
    existing_urls = [e.get("url") for e in data.get("urls", [])]
    if url_val in existing_urls:
        raise HTTPException(status_code=409,
            detail=f"URL already exists in config: {url_val}")

    group_key   = entry.get("group", "general").strip()
    group_label = entry.get("group_label", "").strip()

    new_entry = {
        "url":      url_val,
        "label":    entry.get("label", "").strip(),
        "group":    group_key,
        "type":     entry.get("type", "news"),
        "language": entry.get("language", "en"),
        "notes":    entry.get("notes", "")
    }
    data.setdefault("urls", []).append(new_entry)

    # Persist new custom group into data["groups"] if it doesn't already exist
    data.setdefault("groups", {})
    if group_key and group_key not in data["groups"]:
        # Use provided label, or fall back to prettifying the slug
        data["groups"][group_key] = group_label or group_key.replace("_", " ").title()
        logger.info("[Config] New group added to urls.json: %s = %s",
                    group_key, data["groups"][group_key])

    _write_config_file("urls.json", data)
    return {
        "success": True,
        "urls":    data["urls"],
        "groups":  data["groups"]   # return updated groups so frontend can refresh dropdown
    }


@app.post("/api/config/urls/bulk")
def add_config_urls_bulk(payload: dict):
    """
    Accepts list of URL entries, validates and dedupes them,
    and appends the successful ones to config/urls.json.
    Payload: {"urls": [ {url, label, group, type, language, notes}, ... ]}
    """
    entries = payload.get("urls", [])
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="urls must be a list")
    
    if len(entries) > 500:
        raise HTTPException(status_code=400, detail="Batch exceeds the limit of 500 entries.")

    data = _read_config_file("urls.json")
    existing_urls = {e.get("url") for e in data.get("urls", [])}
    
    added = []
    skipped = []
    seen_in_batch = set()
    
    required = {"url", "label", "group", "type", "language"}
    
    for item in entries:
        missing = required - set(item.keys())
        if not missing:
            empty = [f for f in required if not str(item.get(f, "")).strip()]
            if empty:
                skipped.append({
                    "entry": item,
                    "reason": f"Missing required fields: {', '.join(sorted(empty))}"
                })
                continue
        else:
            skipped.append({
                "entry": item,
                "reason": f"Missing required fields: {', '.join(sorted(missing))}"
            })
            continue

        url_val = item.get("url", "").strip()
        
        if not url_val.startswith(("http://", "https://")):
            skipped.append({
                "entry": item,
                "reason": "invalid URL format"
            })
            continue
            
        if url_val in seen_in_batch:
            skipped.append({
                "entry": item,
                "reason": "duplicate in batch"
            })
            continue
            
        if url_val in existing_urls:
            skipped.append({
                "entry": item,
                "reason": "duplicate"
            })
            continue
            
        group_key = item.get("group", "general").strip()
        group_label = item.get("group_label", "").strip()
        new_entry = {
            "url":      url_val,
            "label":    item.get("label", "").strip(),
            "group":    group_key,
            "type":     item.get("type", "news"),
            "language": item.get("language", "en"),
            "notes":    item.get("notes", "")
        }
        data.setdefault("urls", []).append(new_entry)
        added.append(new_entry)

        # Persist new custom group if it doesn't already exist
        data.setdefault("groups", {})
        if group_key and group_key not in data["groups"]:
            data["groups"][group_key] = group_label or group_key.replace("_", " ").title()
            logger.info("[Config] New group added to urls.json (bulk): %s = %s",
                        group_key, data["groups"][group_key])
        
    if added:
        _write_config_file("urls.json", data)
        
    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "urls": data.get("urls", []),
        "groups": data.get("groups", {})
    }


@app.delete("/api/config/urls/{index}")
def delete_config_url(index: int):
    """
    Deletes the URL entry at position `index` from config/urls.json.
    Returns the updated urls list.
    """
    data = _read_config_file("urls.json")
    urls = data.get("urls", [])
    if index < 0 or index >= len(urls):
        raise HTTPException(status_code=404,
            detail=f"No URL at index {index}.")
    removed = urls.pop(index)
    data["urls"] = urls
    _write_config_file("urls.json", data)
    return {"success": True, "removed": removed, "urls": urls}


@app.put("/api/config/urls/groups/{group_key}")
def update_url_group_label(group_key: str, payload: dict):
    """
    Updates the human-readable label for an existing group in urls.json.
    Payload: { "label": "New Label" }
    Does NOT rename the group key on entries — only updates the display label.
    """
    data = _read_config_file("urls.json")
    if group_key not in data.get("groups", {}):
        raise HTTPException(status_code=404,
            detail=f"Group '{group_key}' not found in urls.json")
    new_label = payload.get("label", "").strip()
    if not new_label:
        raise HTTPException(status_code=422, detail="label cannot be empty.")
    data["groups"][group_key] = new_label
    _write_config_file("urls.json", data)
    return {"success": True, "groups": data["groups"]}


@app.put("/api/config/keywords/groups/{group_key}")
def update_keyword_group_label(group_key: str, payload: dict):
    """
    Updates the human-readable label for an existing group in keywords.json.
    Payload: { "label": "New Label" }
    """
    data = _read_config_file("keywords.json")
    if group_key not in data.get("groups", {}):
        raise HTTPException(status_code=404,
            detail=f"Group '{group_key}' not found in keywords.json")
    new_label = payload.get("label", "").strip()
    if not new_label:
        raise HTTPException(status_code=422, detail="label cannot be empty.")
    data["groups"][group_key] = new_label
    _write_config_file("keywords.json", data)
    return {"success": True, "groups": data["groups"]}


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
    queries = db.scalars(select(SearchQuery).order_by(SearchQuery.created_at.desc())).all()
    return queries

@app.delete("/api/search/{search_id}")
@limiter.limit("10/minute")
def delete_search(request: Request, search_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Deletes a search query record and all its crawled URLs."""
    query = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
    if not query:
        raise HTTPException(status_code=404, detail="Search query not found")
    db.delete(query)
    db.commit()
    return {"message": f"Search run {search_id} successfully deleted"}

@app.post("/api/search/{search_id}/stop")
@limiter.limit("10/minute")
def stop_search(request: Request, search_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Gracefully aborts a search query run."""
    query = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
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
def retry_search(request: Request, search_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Duplicates a past search run config and creates a new pending run."""
    old_query = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
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
    query_record = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
    if not query_record:
        raise HTTPException(status_code=404, detail="Search query not found")

    # Start building base query for CrawledURL
    stmt = select(CrawledURL).where(CrawledURL.search_id == search_id)

    # Apply filters
    if domain_query:
        stmt = stmt.where(CrawledURL.domain.contains(domain_query.lower()))
    if min_relevance is not None:
        stmt = stmt.where(CrawledURL.relevance_score >= min_relevance)

    # Count total matched before paginating
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_count = db.execute(count_stmt).scalar() or 0

    # Paginated results (order by relevance score desc, with error items at bottom)
    results_stmt = stmt.order_by(
        CrawledURL.relevance_score.desc(),
        CrawledURL.discovered_at.desc()
    ).offset((page - 1) * limit).limit(limit)
    results = db.scalars(results_stmt).all()

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
    current_user: User = Depends(get_current_user)
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

    query_record = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
    keyword_clean = "".join(c for c in query_record.keyword if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    filename = f"keyword_results_{search_id}_{keyword_clean}.{format}"
    
    headers = {
        "Content-Disposition": f"attachment; filename={filename}"
    }

    return Response(content=data_bytes, media_type=media_type, headers=headers)

@app.post("/api/export/{search_id}/postgres")
@limiter.limit("10/minute")
def export_search_postgres(request: Request, search_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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
def create_schedule(request: Request, payload: SearchScheduleCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Creates a new recurring keyword search schedule."""
    # Clean keyword
    kw_clean = (payload.keyword or "").strip()
    payload.keyword = kw_clean
    payload.config.keyword = (payload.config.keyword or "").strip()

    # Allow "config" source type — URLs resolved at trigger time
    if payload.config.source_type not in ("search", "direct", "sitemap", "feed", "config"):
        raise HTTPException(status_code=400, detail="Invalid source_type")

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
    schedules = db.scalars(select(SearchSchedule).order_by(SearchSchedule.created_at.desc())).all()
    return schedules

@app.delete("/api/schedules/{schedule_id}")
@limiter.limit("10/minute")
def delete_schedule(request: Request, schedule_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Deletes a schedule."""
    sched = db.scalars(select(SearchSchedule).where(SearchSchedule.id == schedule_id)).first()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(sched)
    db.commit()
    return {"message": f"Schedule {schedule_id} successfully deleted"}

@app.post("/api/scrape", response_model=FirecrawlResponse)
@limiter.limit("10/minute")
def scrape_url(request: Request, payload: ScrapeRequest, current_user: User = Depends(get_current_user)):
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
    crawled_url = db.scalars(select(CrawledURL).where(CrawledURL.id == url_id)).first()
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
