# ## Changes (Trafilatura Integration — KeywordScout v2.0 Upgrade)
# - Added "sitemap" and "feed" source_types to SearchQueryCreate.
# ## Changes
# - Added ignore_robots parameter to SearchQueryCreate schema.
# - Added ignore_robots parameter to SearchQueryResponse schema.

from datetime import datetime
from typing import List, Optional, Dict
from pydantic import BaseModel, ConfigDict

class SearchQueryCreate(BaseModel):
    keyword: Optional[str] = ""
    match_type: Optional[str] = "phrase"  # "phrase" or "boolean"
    case_sensitive: Optional[bool] = False
    exact_match: Optional[bool] = False
    domains_filter: Optional[Dict[str, List[str]]] = None  # {"include": [...], "exclude": [...]}
    languages_filter: Optional[List[str]] = None  # ["en", "es", ...]
    date_range_start: Optional[datetime] = None
    date_range_end: Optional[datetime] = None
    engine: Optional[str] = "fast"  # "fast", "dynamic", or "lightpanda"
    source_type: Optional[str] = "search"  # Allowed: "search", "direct", "sitemap", "feed"
    direct_urls: Optional[str] = None  # Multiline string of raw URLs
    ignore_robots: Optional[bool] = False
    proxy_url: Optional[str] = None  # HTTP/SOCKS5 proxy e.g. "socks5h://host:1080" or "http://host:8080"
    schedule_time_hour: Optional[int] = None
    schedule_time_minute: Optional[int] = None
    schedule_time_weekday: Optional[int] = None
    schedule_time_day: Optional[int] = None

class SearchQueryResponse(BaseModel):
    id: int
    keyword: str
    match_type: str
    case_sensitive: bool
    exact_match: bool
    domains_filter: Optional[str] = None
    languages_filter: Optional[str] = None
    date_range_start: Optional[datetime] = None
    date_range_end: Optional[datetime] = None
    engine: str
    source_type: str
    direct_urls: Optional[str] = None
    ignore_robots: bool
    status: str
    total_urls_found: int
    total_urls_crawled: int
    total_urls_matched: int
    error_message: Optional[str] = None
    status_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class CrawledURLResponse(BaseModel):
    id: int
    search_id: int
    url: str
    domain: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    occurrences: int
    found_in_title: bool
    found_in_description: bool
    found_in_body: bool
    found_in_url: bool
    language: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    relevance_score: float
    is_duplicate: bool
    description: Optional[str] = None
    full_content: Optional[str] = None
    raw_html: Optional[str] = None
    author: Optional[str] = None
    image_url: Optional[str] = None
    image_links: Optional[str] = None
    video_links: Optional[str] = None
    discovered_at: datetime
    matched_keywords: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class PaginatedCrawledURLResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: List[CrawledURLResponse]

class SearchScheduleCreate(BaseModel):
    keyword: Optional[str] = ""
    frequency: str  # "daily", "weekly", "monthly"
    engine: Optional[str] = "fast"
    config: SearchQueryCreate
    next_run: Optional[datetime] = None

class SearchScheduleResponse(BaseModel):
    id: int
    keyword: str
    frequency: str
    active: bool
    engine: str
    config_json: str
    last_run: Optional[datetime] = None
    next_run: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

# Firecrawl Output Schema Mappings
class FirecrawlMetadata(BaseModel):
    title: str = ""
    description: str = ""
    language: str = ""
    sourceURL: str = ""
    statusCode: int = 200

class FirecrawlImageData(BaseModel):
    src: str = ""
    alt: str = ""
    caption: str = ""
    width: int = 0
    height: int = 0

class FirecrawlVideoData(BaseModel):
    src: str = ""
    title: str = ""
    thumbnail: str = ""
    type: str = ""

class FirecrawlLinkData(BaseModel):
    text: str = ""
    url: str = ""
    title: str = ""

class FirecrawlContent(BaseModel):
    headings: List[str] = []
    paragraphs: List[str] = []
    lists: List[List[str]] = []
    tables: List[List[List[str]]] = []
    codeBlocks: List[str] = []
    quotes: List[str] = []

class FirecrawlData(BaseModel):
    markdown: str = ""
    html: str = ""
    metadata: FirecrawlMetadata
    links: List[FirecrawlLinkData] = []
    images: List[FirecrawlImageData] = []
    videos: List[FirecrawlVideoData] = []
    content: FirecrawlContent

class FirecrawlResponse(BaseModel):
    success: bool = True
    data: FirecrawlData

class ScrapeRequest(BaseModel):
    url: str
    engine: Optional[str] = "fast"
    ignore_robots: Optional[bool] = False
    proxy_url: Optional[str] = None  # HTTP/SOCKS5 proxy for geo-restricted sites


class UserCreate(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str
