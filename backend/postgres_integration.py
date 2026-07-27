import os
import re
import json
import time
import logging
import hashlib
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Tuple, Dict, Any
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, text, inspect, select, Index
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import UUID, insert
from sqlalchemy.exc import OperationalError, DBAPIError

from backend.models import CrawledURL, SearchQuery

# Setup module logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("postgres_integration")

# Load environment variables
load_dotenv()

# Read target PostgreSQL database credentials from environment
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "10.10.116.170")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "35432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "fortress_ntxx_db")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "anjali")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "AnjaliModule2024!")
POSTGRES_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "news_media")

# Encode password to prevent special character connection errors
encoded_password = urllib.parse.quote_plus(POSTGRES_PASSWORD)

# Construct connection URL targeting the specific schema
POSTGRES_URL = f"postgresql://{POSTGRES_USER}:{encoded_password}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}?options=-csearch_path%3D{POSTGRES_SCHEMA}"

# Connection engine and session maker with connection pooling and timeouts
try:
    engine = create_engine(
        POSTGRES_URL,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_pre_ping=True,       # Test connection liveness before each checkout
        connect_args={
            "connect_timeout": 10,          # TCP connect timeout
            "application_name": "keywordscout_sync",  # Visible in pg_stat_activity
        }
    )
    SessionPostgres = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logger.info("[PostgreSQL] Connection engine initialized successfully.")
except Exception as init_err:
    logger.error(f"[PostgreSQL] Failed to initialize connection engine: {init_err}")
    raise init_err

Base = declarative_base()


# PostgreSQL ORM Model for news_media.scraped_news
class ScrapedNews(Base):
    __tablename__ = "scraped_news"
    __table_args__ = (
        Index("idx_scraped_news_keyword", "keyword"),
        Index("idx_scraped_news_published_date", "published_date"),
        Index("idx_scraped_news_source", "source"),
        Index("idx_scraped_news_status", "status"),
        Index("idx_scraped_news_crawl_id", "crawl_id"),
        {"schema": POSTGRES_SCHEMA}
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(Text, nullable=False)
    title = Column(Text)
    url = Column(Text, unique=True)
    source = Column(Text)
    author = Column(Text)
    published_date = Column(DateTime)
    scraped_date = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), default=lambda: datetime.now(timezone.utc))
    language = Column(String(20))
    country = Column(String(50))
    summary = Column(Text)
    content = Column(Text)
    sentiment = Column(String(20))
    category = Column(String(100))
    image_url = Column(Text)
    crawl_id = Column(UUID(as_uuid=True))
    status = Column(String(20))
    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# Heuristic classification helper
def classify_article(url: str, title: str, full_content: str, language_code: str) -> Dict[str, str]:
    """
    Heuristically classifies the article/web page into:
    - source_type: News, Journal, Think Tank, Government, Research Institute
    - content_type: Article, Report, Policy Brief, Journal, Event, Book, Podcast, Video
    - subject_theme: Maritime, Defence, Security, Politics, Economy, etc.
    - country_region: Countries/Regions covered
    - language: English, Urdu, Burmese, etc.
    """
    parsed_url = urllib.parse.urlparse(url)
    domain = parsed_url.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    title_lower = title.lower() if title else ""
    content_lower = full_content.lower() if full_content else ""
    text_to_check = f"{title_lower} {content_lower}"
    url_lower = url.lower()

    # Heuristic A: source_type
    source_type = "News"
    if ".gov" in domain:
        source_type = "Government"
    elif ".edu" in domain or ".ac." in domain:
        source_type = "Research Institute"
    elif any(kw in domain or kw in url_lower for kw in ["journal", "springer", "ieee", "sciencedirect", "nature", "academic", "researchgate"]):
        source_type = "Journal"
    elif any(kw in domain or kw in url_lower for kw in ["thinktank", "brookings", "rand", "csis", "chathamhouse", "cfr", "rusi", "sipri", "iiss", "lowyinstitute"]):
        source_type = "Think Tank"

    # Heuristic B: content_type
    content_type = "Article"
    if url_lower.endswith(".pdf") or "pdf" in url_lower:
        if any(kw in url_lower or kw in title_lower for kw in ["policy", "brief", "memo", "advisory", "recommendation"]):
            content_type = "Policy Brief"
        else:
            content_type = "Report"
    elif any(kw in url_lower or kw in title_lower for kw in ["podcast", "audio", "listen", "soundcloud"]):
        content_type = "Podcast"
    elif any(kw in url_lower or kw in title_lower for kw in ["video", "youtube", "vimeo", "watch", "broadcast", "clip"]):
        content_type = "Video"
    elif any(kw in url_lower or kw in title_lower for kw in ["event", "webinar", "conference", "summit", "workshop", "seminar"]):
        content_type = "Event"
    elif any(kw in url_lower or kw in title_lower for kw in ["book", "monograph", "novel"]):
        content_type = "Book"
    elif source_type == "Journal":
        content_type = "Journal"

    # Heuristic C: subject_theme
    themes = []
    if any(kw in text_to_check for kw in ["sea", "port", "vessel", "ship", "maritime", "ocean", "naval", "shipping", "harbor", "strait", "submarine"]):
        themes.append("Maritime")
    if any(kw in text_to_check for kw in ["army", "weapon", "missile", "military", "defence", "defense", "soldier", "navy", "war", "combat", "arsenal", "ammunition"]):
        themes.append("Defence")
    if any(kw in text_to_check for kw in ["security", "cyber", "threat", "intelligence", "police", "attack", "terrorism", "espionage", "surveillance"]):
        themes.append("Security")
    if any(kw in text_to_check for kw in ["politics", "election", "government", "parliament", "vote", "senate", "congress", "policy", "regime", "geopolitics", "diplomatic"]):
        themes.append("Politics")
    if any(kw in text_to_check for kw in ["economy", "trade", "gdp", "finance", "fiscal", "inflation", "market", "tariff", "economic", "business", "commerce"]):
        themes.append("Economy")

    subject_theme = ", ".join(themes) if themes else "General"

    # Heuristic D: country_region
    countries = []
    country_list = [
        "India", "China", "United States", "USA", "Pakistan", "Myanmar", "Burma", "Russia", "Japan", 
        "Taiwan", "Iran", "North Korea", "South Korea", "Vietnam", "Philippines", "Indonesia", "Malaysia", 
        "Singapore", "Thailand", "Bangladesh", "Sri Lanka", "Maldives", "Australia", "Ukraine", "United Kingdom", "UK"
    ]
    for country in country_list:
        pattern = rf"\b{re.escape(country.lower())}\b"
        if re.search(pattern, text_to_check):
            countries.append(country)

    # Special case-sensitive check for US / U.S. abbreviation
    raw_text = f"{title} {full_content}" if (title or full_content) else ""
    if re.search(r"\bUS\b|\bU\.S\.\b", raw_text):
        if "United States" not in countries:
            countries.append("United States")

    regions = []
    region_list = ["Indo-Pacific", "Asia-Pacific", "South Asia", "Southeast Asia", "Middle East", "Europe", "Africa", "Americas"]
    for region in region_list:
        pattern = rf"\b{re.escape(region.lower())}\b"
        if re.search(pattern, text_to_check):
            regions.append(region)

    geo_covered = ", ".join(countries + regions) if (countries or regions) else "Global"

    # Heuristic E: language
    lang_map = {
        "en": "English",
        "ur": "Urdu",
        "my": "Burmese",
        "hi": "Hindi",
        "zh": "Chinese",
        "ru": "Russian",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "ja": "Japanese",
        "ar": "Arabic"
    }
    language = lang_map.get(language_code.lower() if language_code else "en", "English")

    return {
        "source_type": source_type,
        "content_type": content_type,
        "subject_theme": subject_theme,
        "country_region": geo_covered,
        "language": language
    }


def analyze_sentiment(title: str, content: str) -> str:
    """
    Performs simple word-list heuristic sentiment analysis.
    """
    text_val = f"{title or ''} {content or ''}".lower()
    pos_words = {"positive", "success", "growth", "win", "alliance", "cooperation", "support", "benefit", "strengthen", "advance", "develop"}
    neg_words = {"negative", "fail", "crisis", "threat", "conflict", "warn", "clash", "tension", "dispute", "protest", "risk", "damage"}
    
    pos_count = sum(1 for word in pos_words if word in text_val)
    neg_count = sum(1 for word in neg_words if word in text_val)
    
    if pos_count > neg_count:
        return "positive"
    elif neg_count > pos_count:
        return "negative"
    return "neutral"


# Database initialization check
def init_postgres_db(verbose: bool = True):
    """
    Initializes target PostgreSQL schema, table, and indexes.
    Checks and creates schema first, then table metadata, then explicit indexes.
    """
    conn = None
    try:
        # Connect to verify availability
        conn = engine.connect()
        logger.info("[PostgreSQL] Connection pool test succeeded.")
        
        # 1. Ensure target schema exists
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA};"))
        conn.commit()
        if verbose:
            print(f"[PostgreSQL] Ensured schema '{POSTGRES_SCHEMA}' exists.")
            logger.info(f"Schema '{POSTGRES_SCHEMA}' verified/created.")
            
        # 2. Create the scraped_news table structure
        Base.metadata.create_all(bind=engine)
        if verbose:
            print(f"[PostgreSQL] Table 'scraped_news' initialized in schema '{POSTGRES_SCHEMA}'.")
            
        # 3. Create indexes only if they do not already exist
        indexes_to_create = [
            ("idx_scraped_news_keyword", "keyword"),
            ("idx_scraped_news_published_date", "published_date"),
            ("idx_scraped_news_source", "source"),
            ("idx_scraped_news_status", "status"),
            ("idx_scraped_news_crawl_id", "crawl_id")
        ]
        
        for idx_name, column in indexes_to_create:
            try:
                # Query PostgreSQL catalog to check if index exists
                query = text(f"""
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relname = '{idx_name}' AND n.nspname = '{POSTGRES_SCHEMA}';
                """)
                res = conn.execute(query).fetchone()
                if not res:
                    conn.execute(text(f"CREATE INDEX {idx_name} ON {POSTGRES_SCHEMA}.scraped_news ({column});"))
                    conn.commit()
                    logger.info(f"[PostgreSQL] Created index {idx_name} on {POSTGRES_SCHEMA}.scraped_news ({column}).")
            except Exception as index_err:
                logger.warning(f"[PostgreSQL Warning] Could not verify or create index {idx_name}: {index_err}")
                
        # 4. Dynamic migration / structure check
        try:
            inspector = inspect(engine)
            table_names = inspector.get_table_names(schema=POSTGRES_SCHEMA)
            if 'scraped_news' in table_names:
                columns = [col['name'] for col in inspector.get_columns('scraped_news', schema=POSTGRES_SCHEMA)]
                logger.info(f"[PostgreSQL] Migration check: table 'scraped_news' verified with columns: {columns}")
        except Exception as mig_err:
            logger.warning(f"[PostgreSQL Warning] Dynamic migration check failed: {mig_err}")
            
    except OperationalError as oe:
        logger.error(f"[PostgreSQL Connection Failure] Database is unavailable or timed out: {oe}")
        raise oe
    except Exception as e:
        logger.error(f"[PostgreSQL Error] Failed to generate table structures: {e}")
        raise e
    finally:
        if conn:
            conn.close()


# PostgreSQL CRUD and Synchronizer
def export_search_to_postgres(search_id: int, db_session) -> Tuple[int, int]:
    """
    Fetches matching CrawledURL entries for the search_id, classifies and analyzes them,
    and inserts/upserts them into news_media.scraped_news with native ON CONFLICT UPSERT.
    Returns: (inserted_count, updated_count)
    """
    records = db_session.scalars(select(CrawledURL).where(
        CrawledURL.search_id == search_id,
        CrawledURL.status == "matched"
    )).all()

    search_query_record = db_session.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
    query_keyword = search_query_record.keyword if search_query_record else ""

    # Generate a stable UUID based on search_id for crawl_id
    crawl_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"search_query_{search_id}")

    pg_db = SessionPostgres()
    upserted_count = 0
    failed_count = 0

    try:
        for r in records:
            # Skip if URL is missing
            if not r.url:
                continue

            # Classify page heuristically
            classification = classify_article(r.url, r.title, r.full_content, r.language)
            sentiment_val = analyze_sentiment(r.title, r.full_content)

            # Map matched keywords
            matched_kws = ""
            if r.matched_keywords:
                try:
                    kws = json.loads(r.matched_keywords)
                    if isinstance(kws, list):
                        matched_kws = ", ".join(kws)
                except Exception:
                    pass
            if not matched_kws:
                matched_kws = query_keyword or "Unknown"

            # Construct PostgreSQL UPSERT statement
            stmt = insert(ScrapedNews).values(
                keyword=matched_kws,
                title=r.title or "Untitled",
                url=r.url,
                source=r.domain or "Unknown",
                author=r.author or "Unknown",
                published_date=r.discovered_at or datetime.now(timezone.utc),
                scraped_date=datetime.now(timezone.utc),
                language=(classification.get("language") or r.language or "English")[:20],
                country=(classification.get("country_region") or "Unknown")[:50],
                summary=(r.description or r.snippet or "")[:5000],
                content=r.full_content or "",
                sentiment=sentiment_val,
                category=(classification.get("subject_theme") or "General")[:100],
                image_url=r.image_url,
                crawl_id=crawl_uuid,
                status=r.status or "matched",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )

            # Define ON CONFLICT clause targeting unique 'url' constraint
            stmt = stmt.on_conflict_do_update(
                index_elements=["url"],
                set_={
                    "keyword": stmt.excluded.keyword,
                    "title": stmt.excluded.title,
                    "source": stmt.excluded.source,
                    "author": stmt.excluded.author,
                    "published_date": stmt.excluded.published_date,
                    "scraped_date": stmt.excluded.scraped_date,
                    "language": stmt.excluded.language,
                    "country": stmt.excluded.country,
                    "summary": stmt.excluded.summary,
                    "content": stmt.excluded.content,
                    "sentiment": stmt.excluded.sentiment,
                    "category": stmt.excluded.category,
                    "image_url": stmt.excluded.image_url,
                    "crawl_id": stmt.excluded.crawl_id,
                    "status": stmt.excluded.status,
                    "updated_at": stmt.excluded.updated_at
                }
            )

            # Robust single-record execution with retry attempts on connection drops
            max_retries = 3
            success = False
            for attempt in range(1, max_retries + 1):
                try:
                    pg_db.execute(stmt)
                    pg_db.commit()
                    upserted_count += 1
                    success = True
                    logger.info(f"[PostgreSQL Sync] Successfully upserted article: {r.url}")
                    break
                except (OperationalError, DBAPIError) as conn_err:
                    pg_db.rollback()
                    logger.warning(f"[PostgreSQL Sync Warning] Connection issue on attempt {attempt} for URL {r.url}: {conn_err}")
                    if attempt == max_retries:
                        logger.error(f"[PostgreSQL Sync Error] Failed to sync article after {max_retries} attempts: {r.url}")
                        failed_count += 1
                        raise conn_err
                    time.sleep(1) # Backoff before retry
                except Exception as db_err:
                    pg_db.rollback()
                    logger.error(f"[PostgreSQL Sync Error] Non-retryable database transaction failure for URL {r.url}: {db_err}")
                    failed_count += 1
                    break # Do not retry on programming errors

        logger.info(f"[PostgreSQL Sync Completed] Synced search {search_id}. Total upserts: {upserted_count}, failed: {failed_count}")
        return upserted_count, 0 # We report upserted_count as inserted
        
    except Exception as e:
        pg_db.rollback()
        logger.error(f"[PostgreSQL Sync Exception] Sync failed for search {search_id}: {e}")
        raise e
    finally:
        pg_db.close()
