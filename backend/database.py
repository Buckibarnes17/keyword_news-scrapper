import os
import urllib.parse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Load environment variables from .env file
load_dotenv()

import socket

def is_postgres_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

# Read target PostgreSQL database URL configuration dynamically
DATABASE_URL = os.environ.get("DATABASE_URL")
POSTGRES_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "news_media")

if not DATABASE_URL:
    POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "10.10.116.170")
    POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "35432")
    POSTGRES_DB = os.environ.get("POSTGRES_DB", "fortress_ntxx_db")
    POSTGRES_USER = os.environ.get("POSTGRES_USER", "anjali")
    POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "AnjaliModule2024!")

    # Encode password in case of special characters
    encoded_password = urllib.parse.quote_plus(POSTGRES_PASSWORD)

    # Check PostgreSQL reachability with a 2-second timeout before constructing the connection string
    if is_postgres_reachable(POSTGRES_HOST, int(POSTGRES_PORT), timeout=2.0):
        DATABASE_URL = f"postgresql://{POSTGRES_USER}:{encoded_password}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}?options=-csearch_path%3D{POSTGRES_SCHEMA}"
        print(f"[Database] Connected to PostgreSQL: {POSTGRES_HOST}:{POSTGRES_PORT}")
    else:
        print(f"[Database Fallback] PostgreSQL database at {POSTGRES_HOST}:{POSTGRES_PORT} is unreachable. Falling back to local SQLite.")
        if os.environ.get("VERCEL") == "1" or "VERCEL" in os.environ:
            DATABASE_URL = "sqlite:////tmp/keywordscout.db"
        else:
            DATABASE_URL = "sqlite:///keywordscout.db"
else:
    # If DATABASE_URL is provided, verify it is reachable if it's a PostgreSQL string
    if DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://"):
        try:
            parsed = urllib.parse.urlparse(DATABASE_URL)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 5432
            if not is_postgres_reachable(host, port, timeout=2.0):
                print(f"[Database Fallback] Configured PostgreSQL database at {host}:{port} is unreachable. Falling back to local SQLite.")
                if os.environ.get("VERCEL") == "1" or "VERCEL" in os.environ:
                    DATABASE_URL = "sqlite:////tmp/keywordscout.db"
                else:
                    DATABASE_URL = "sqlite:///keywordscout.db"
        except Exception as e:
            print(f"[Database Warning] Error checking configured database liveness: {e}")

# Connect with a robust pool size and timeout configurations suitable for web apps
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30}
    )
    
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()
else:
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_pre_ping=True,       # Test connection liveness before each checkout
        connect_args={
            "connect_timeout": 10,          # TCP connect timeout
            "application_name": "keywordscout",  # Visible in pg_stat_activity
        }
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    """
    Checks if the target database exists on the PostgreSQL server.
    If not, connects to the administrative 'postgres' database and creates it.
    Then executes SQLAlchemy metadata generation and column migrations.
    """
    if not DATABASE_URL.startswith("sqlite"):
        try:
            parsed = urllib.parse.urlparse(DATABASE_URL)
            db_name = parsed.path.lstrip('/')
            
            if db_name:
                # Connect to admin database to verify target database existence
                postgres_default_url = urllib.parse.urlunparse(
                    parsed._replace(path='/postgres')
                )
                admin_engine = create_engine(postgres_default_url, isolation_level="AUTOCOMMIT")
                try:
                    with admin_engine.connect() as admin_conn:
                        res = admin_conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname='{db_name}'")).fetchone()
                        if not res:
                            admin_conn.execute(text(f"CREATE DATABASE {db_name}"))
                            print(f"[PostgreSQL] Successfully created database '{db_name}'.")
                except Exception as admin_err:
                    print(f"[PostgreSQL Warning] Fallback database check failed: {admin_err}")
                finally:
                    admin_engine.dispose()
        except Exception as e:
            print(f"[PostgreSQL Warning] Database precheck error: {e}")

        # Ensure the target schema exists
        try:
            with engine.connect() as conn:
                conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA};"))
                conn.commit()
                print(f"[PostgreSQL] Ensured schema '{POSTGRES_SCHEMA}' exists.")
        except Exception as schema_err:
            print(f"[PostgreSQL Warning] Error ensuring schema '{POSTGRES_SCHEMA}' exists: {schema_err}")

    # Create tables and run dynamic schema migrations
    try:
        from backend.models import User, SearchQuery, CrawledURL, SearchSchedule, KeywordProgress
        Base.metadata.create_all(bind=engine)
        if DATABASE_URL.startswith("sqlite"):
            print("[SQLite] Successfully initialized/verified database tables.")
        else:
            print("[PostgreSQL] Successfully initialized/verified database tables.")
        
        # Run dynamic schema migrations using the inspector
        from sqlalchemy import inspect
        inspector = inspect(engine)
        
        if 'crawled_urls' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('crawled_urls')]
            if 'matched_keywords' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN matched_keywords TEXT NULL;"))
                print("Database migration: added matched_keywords column to crawled_urls.")
            if 'content_hash' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN content_hash VARCHAR(255) NULL;"))
                print("Database migration: added content_hash column to crawled_urls.")
            if 'description' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN description TEXT NULL;"))
                print("Database migration: added description column to crawled_urls.")
            if 'full_content' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN full_content TEXT NULL;"))
                print("Database migration: added full_content column to crawled_urls.")
            if 'raw_html' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN raw_html TEXT NULL;"))
                print("Database migration: added raw_html column to crawled_urls.")
            if 'author' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN author VARCHAR(255) NULL;"))
                print("Database migration: added author column to crawled_urls.")
            if 'image_url' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN image_url TEXT NULL;"))
                print("Database migration: added image_url column to crawled_urls.")
            if 'image_links' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN image_links TEXT NULL;"))
                print("Database migration: added image_links column to crawled_urls.")
            if 'video_links' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN video_links TEXT NULL;"))
                print("Database migration: added video_links column to crawled_urls.")
            if 'simhash' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE crawled_urls ADD COLUMN simhash VARCHAR(16) NULL;"))
                print("Database migration: added simhash column to crawled_urls.")
                
        if 'search_queries' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('search_queries')]
            if 'ignore_robots' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE search_queries ADD COLUMN ignore_robots BOOLEAN DEFAULT FALSE;"))
                print("Database migration: added ignore_robots column to search_queries.")
            if 'proxy_url' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE search_queries ADD COLUMN proxy_url TEXT NULL;"))
                print("Database migration: added proxy_url column to search_queries.")
            if 'status_message' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE search_queries ADD COLUMN status_message TEXT NULL;"))
                print("Database migration: added status_message column to search_queries.")

        if 'search_schedules' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('search_schedules')]
            if 'engine' not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE search_schedules ADD COLUMN engine VARCHAR(50) DEFAULT 'fast';"))
                print("Database migration: added engine column to search_schedules.")
    except Exception as db_init_err:
        print(f"[Database Error] Failed to initialize table schema and migrations: {db_init_err}")

from fastapi import Request

def get_db(request: Request = None):
    db = SessionLocal()
    if request is not None:
        request.state.db = db
    try:
        yield db
    finally:
        db.close()
