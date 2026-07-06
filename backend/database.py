import os
import urllib.parse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Load environment variables from .env file
load_dotenv()

# Read target PostgreSQL database URL configuration
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/keyword_scraper"
)

# Connect with a robust pool size and timeout configurations suitable for web apps
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

    # Create tables
    Base.metadata.create_all(bind=engine)
    
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

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
