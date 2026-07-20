import os
import re
import json
import hashlib
import urllib.parse
from datetime import datetime, timezone
from typing import Tuple, Dict, Any
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, String, Text, DateTime, text, inspect, select
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError

from backend.models import CrawledURL, SearchQuery

# Load environment variables
load_dotenv()

# 1. Database URL configuration
POSTGRES_URL = os.environ.get(
    "DATABASE_URL",
    os.environ.get(
        "POSTGRES_URL",
        "postgresql://postgres:postgres@localhost:5432/keyword_scraper"
    )
)


# 2. Connection engine and session maker
engine = create_engine(
    POSTGRES_URL,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800
)
SessionPostgres = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# 3. PostgreSQL Model definition
class ScrapedArticle(Base):
    __tablename__ = "scraped_articles"

    record_id = Column(String(255), primary_key=True)
    source_name = Column(String(255), nullable=False)
    source_type = Column(String(100))
    title = Column(Text)
    url = Column(Text)
    publication_date = Column(DateTime(timezone=True))
    author = Column(String(255))
    content_type = Column(String(100))
    subject_theme = Column(Text)
    country_region = Column(String(255))
    language = Column(String(100))
    keywords = Column(Text)
    full_text = Column(Text)
    raw_html = Column(Text)
    tags = Column(Text)
    pdf_link = Column(Text)
    image_links = Column(Text)
    video_links = Column(Text)
    organization = Column(String(255))
    scraped_date = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# 4. Heuristic classification helper
def classify_article(url: str, title: str, full_content: str, language_code: str) -> Dict[str, str]:
    """
    Heuristically classifies the article/web page into:
    - source_type: News, Journal, Think Tank, Government, Research Institute
    - content_type: Article, Report, Policy Brief, Journal, Event, Book, Podcast, Video
    - subject_theme: Maritime, Defence, Security, Politics, Economy, etc.
    - country_region: Countries/Regions covered
    - language: English, Urdu, Burmese, etc.
    """
    # Initialize URL/text references
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

    # Special case-sensitive check for US / U.S. abbreviation to prevent false matches on the pronoun "us"
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


# 5. Database initialization check
def init_postgres_db(verbose: bool = True):
    """
    Initializes the PostgreSQL database and table.
    Creates the database itself if it doesn't already exist on the server.
    """
    try:
        # Connect first to 'postgres' administrative database to verify/create target database
        parsed = urllib.parse.urlparse(POSTGRES_URL)
        db_name = parsed.path.lstrip('/')
        
        if db_name:
            postgres_default_url = urllib.parse.urlunparse(
                parsed._replace(path='/postgres')
            )
            
            # Use AUTOCOMMIT so we can run database creation commands
            admin_engine = create_engine(postgres_default_url, isolation_level="AUTOCOMMIT")
            try:
                with admin_engine.connect() as admin_conn:
                    res = admin_conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname='{db_name}'")).fetchone()
                    if not res:
                        admin_conn.execute(text(f"CREATE DATABASE {db_name}"))
                        if verbose:
                            print(f"[PostgreSQL] Successfully created database '{db_name}'.")
            except Exception as admin_err:
                if verbose:
                    print(f"[PostgreSQL] Checking database creation using fallback: {admin_err}")
            finally:
                admin_engine.dispose()
    except Exception as e:
        if verbose:
            print(f"[PostgreSQL warning] Error checking database existence: {e}")

    try:
        # Run table generation metadata
        Base.metadata.create_all(bind=engine)
        if verbose:
            print("[PostgreSQL] Tables initialized successfully.")
        
        # Dynamic migration for missing raw_html column
        try:
            inspector = inspect(engine)
            columns = [col['name'] for col in inspector.get_columns('scraped_articles')]
            if 'raw_html' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE scraped_articles ADD COLUMN raw_html TEXT NULL;"))
                if verbose:
                    print("[PostgreSQL] Dynamic migration: Added raw_html column to scraped_articles table.")
        except Exception as mig_err:
            if verbose:
                print(f"[PostgreSQL Warning] Dynamic migration for raw_html failed: {mig_err}")
    except Exception as e:
        print(f"[PostgreSQL Error] Failed to generate table structures: {e}")
        raise e


# 6. Database data synchronizer
def export_search_to_postgres(search_id: int, db_session) -> Tuple[int, int]:
    """
    Fetches matching CrawledURL entries for the search_id from PostgreSQL,
    classifies them, and saves/upserts them to the conformed scraped_articles table.
    Returns: (inserted_count, updated_count)
    """
    records = db_session.scalars(select(CrawledURL).where(
        CrawledURL.search_id == search_id,
        CrawledURL.status == "matched"
    )).all()

    search_query_record = db_session.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
    query_keyword = search_query_record.keyword if search_query_record else ""

    pg_db = SessionPostgres()
    inserted_count = 0
    updated_count = 0

    try:
        for r in records:
            # Generate stable record_id using md5 of URL
            record_id = hashlib.md5(r.url.encode("utf-8")).hexdigest()

            # Classify page heuristically
            classification = classify_article(r.url, r.title, r.full_content, r.language)

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
                matched_kws = query_keyword

            # Check if record already exists in PostgreSQL
            db_article = pg_db.scalars(select(ScrapedArticle).where(ScrapedArticle.record_id == record_id)).first()
            is_new = False
            if not db_article:
                db_article = ScrapedArticle(record_id=record_id)
                is_new = True

            # Update/set fields
            db_article.source_name = r.domain or "Unknown"
            db_article.source_type = classification["source_type"]
            db_article.title = r.title or "Untitled"
            db_article.url = r.url
            db_article.publication_date = r.discovered_at or datetime.now(timezone.utc)
            db_article.author = r.author or "Unknown"
            db_article.content_type = classification["content_type"]
            db_article.subject_theme = classification["subject_theme"]
            db_article.country_region = classification["country_region"]
            db_article.language = classification["language"]
            db_article.keywords = matched_kws
            db_article.full_text = r.full_content or ""
            db_article.raw_html = r.raw_html or ""
            db_article.tags = query_keyword
            db_article.pdf_link = r.url if (r.url and r.url.lower().endswith(".pdf")) else ""
            db_article.image_links = r.image_links or ""
            db_article.video_links = r.video_links or ""
            db_article.organization = r.domain or "Unknown"
            db_article.scraped_date = datetime.now(timezone.utc)

            if is_new:
                pg_db.add(db_article)
                inserted_count += 1
            else:
                updated_count += 1

        pg_db.commit()
        return inserted_count, updated_count
    except Exception as e:
        pg_db.rollback()
        print(f"[PostgreSQL Sync Error] Failed transaction: {e}")
        raise e
    finally:
        pg_db.close()
