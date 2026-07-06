# ## Changes
# - Added content_hash column to CrawledURL model.
# - Updated all DateTime columns to include timezone=True.
# - Replaced datetime.utcnow with datetime.now(timezone.utc).

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from backend.database import Base

class SearchQuery(Base):
    __tablename__ = "search_queries"

    id = Column(Integer, primary_key=True, index=True)
    keyword = Column(String, nullable=False, index=True)
    match_type = Column(String, default="phrase")  # "phrase" or "boolean"
    case_sensitive = Column(Boolean, default=False)
    exact_match = Column(Boolean, default=False)
    domains_filter = Column(Text, nullable=True)  # JSON-serialized inclusion/exclusion list
    languages_filter = Column(Text, nullable=True)  # JSON-serialized language list
    date_range_start = Column(DateTime(timezone=True), nullable=True)
    date_range_end = Column(DateTime(timezone=True), nullable=True)
    engine = Column(String, default="fast")  # "fast" (requests) or "dynamic" (selenium)
    source_type = Column(String, default="search")  # "search" (engine scrape) or "direct" (custom list)
    direct_urls = Column(Text, nullable=True)  # Multiline list of raw URLs/sitemaps
    ignore_robots = Column(Boolean, default=False)
    proxy_url = Column(Text, nullable=True)
    status = Column(String, default="pending")  # "pending", "processing", "completed", "failed"
    total_urls_found = Column(Integer, default=0)
    total_urls_crawled = Column(Integer, default=0)
    total_urls_matched = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    crawled_urls = relationship("CrawledURL", back_populates="search_query", cascade="all, delete-orphan")
    keyword_progress = relationship("KeywordProgress", back_populates="search_query", cascade="all, delete-orphan")


class CrawledURL(Base):
    __tablename__ = "crawled_urls"

    id = Column(Integer, primary_key=True, index=True)
    search_id = Column(Integer, ForeignKey("search_queries.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(Text, nullable=False)
    domain = Column(String, nullable=False, index=True)
    title = Column(String, nullable=True)
    snippet = Column(Text, nullable=True)
    occurrences = Column(Integer, default=0)
    found_in_title = Column(Boolean, default=False)
    found_in_description = Column(Boolean, default=False)
    found_in_body = Column(Boolean, default=False)
    found_in_url = Column(Boolean, default=False)
    language = Column(String, nullable=True)
    status = Column(String, default="pending")  # "pending", "crawling", "matched", "skipped", "failed"
    error_message = Column(Text, nullable=True)
    relevance_score = Column(Float, default=0.0)
    is_duplicate = Column(Boolean, default=False)
    content_hash = Column(String, nullable=True, index=True)
    simhash = Column(String(16), nullable=True, index=True)
    description = Column(Text, nullable=True)
    full_content = Column(Text, nullable=True)
    raw_html = Column(Text, nullable=True)
    author = Column(String, nullable=True)
    image_url = Column(Text, nullable=True)
    image_links = Column(Text, nullable=True)
    video_links = Column(Text, nullable=True)
    discovered_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    matched_keywords = Column(Text, nullable=True)

    # Relationships
    search_query = relationship("SearchQuery", back_populates="crawled_urls")


class SearchSchedule(Base):
    __tablename__ = "search_schedules"

    id = Column(Integer, primary_key=True, index=True)
    keyword = Column(String, nullable=False)
    frequency = Column(String, nullable=False)  # "daily", "weekly", "monthly"
    active = Column(Boolean, default=True)
    engine = Column(String, default="fast")  # "fast" or "dynamic"
    config_json = Column(Text, nullable=False)  # JSON-serialized filter parameters
    last_run = Column(DateTime(timezone=True), nullable=True)
    next_run = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class KeywordProgress(Base):
    __tablename__ = "keyword_progress"

    id = Column(Integer, primary_key=True, index=True)
    search_query_id = Column(Integer, ForeignKey("search_queries.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword = Column(String, nullable=False, index=True)
    status = Column(String, default="pending")  # "pending", "processing", "completed", "failed"
    articles_found = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    search_query = relationship("SearchQuery", back_populates="keyword_progress")
