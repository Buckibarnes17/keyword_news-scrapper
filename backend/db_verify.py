import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy import text, select
from backend.postgres_integration import engine, SessionPostgres, ScrapedNews, POSTGRES_SCHEMA

# Configure verification logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("db_verify")

def verify_integration() -> dict:
    """
    Verifies the PostgreSQL database connection, schema, table, indexes,
    and performs a read-write-delete lifecycle test.
    """
    report = {
        "postgres_connection": False,
        "schema_exists": False,
        "table_exists": False,
        "indexes_exist": False,
        "insert_record": False,
        "read_record": False,
        "delete_record": False,
        "overall_status": "FAILED",
        "details": {}
    }
    
    conn = None
    session = None
    try:
        # 1. PostgreSQL Connection Check
        logger.info("[Verification] Testing connection...")
        conn = engine.connect()
        conn.execute(text("SELECT 1;"))
        report["postgres_connection"] = True
        logger.info("[Verification] Connection successful.")
        
        # 2. Schema Existence Check
        logger.info(f"[Verification] Checking if schema '{POSTGRES_SCHEMA}' exists...")
        schema_query = text("""
            SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema;
        """)
        schema_res = conn.execute(schema_query, {"schema": POSTGRES_SCHEMA}).fetchone()
        if schema_res:
            report["schema_exists"] = True
            logger.info(f"[Verification] Schema '{POSTGRES_SCHEMA}' exists.")
        else:
            report["details"]["schema"] = f"Schema '{POSTGRES_SCHEMA}' not found."
            logger.error(f"[Verification] Schema '{POSTGRES_SCHEMA}' does NOT exist.")
            
        # 3. Table Existence Check
        logger.info(f"[Verification] Checking if table 'scraped_news' exists in schema '{POSTGRES_SCHEMA}'...")
        table_query = text("""
            SELECT 1 FROM information_schema.tables 
            WHERE table_schema = :schema AND table_name = 'scraped_news';
        """)
        table_res = conn.execute(table_query, {"schema": POSTGRES_SCHEMA}).fetchone()
        if table_res:
            report["table_exists"] = True
            logger.info("[Verification] Table 'scraped_news' exists.")
        else:
            report["details"]["table"] = "Table 'scraped_news' not found."
            logger.error("[Verification] Table 'scraped_news' does NOT exist.")
            
        # 4. Indexes Existence Check
        logger.info("[Verification] Checking table indexes...")
        expected_indexes = {
            "idx_scraped_news_keyword",
            "idx_scraped_news_published_date",
            "idx_scraped_news_source",
            "idx_scraped_news_status",
            "idx_scraped_news_crawl_id"
        }
        index_query = text("""
            SELECT indexname FROM pg_indexes 
            WHERE schemaname = :schema AND tablename = 'scraped_news';
        """)
        index_rows = conn.execute(index_query, {"schema": POSTGRES_SCHEMA}).fetchall()
        found_indexes = {row[0] for row in index_rows}
        missing_indexes = expected_indexes - found_indexes
        if not missing_indexes:
            report["indexes_exist"] = True
            logger.info("[Verification] All expected indexes are present.")
        else:
            report["details"]["indexes"] = f"Missing indexes: {list(missing_indexes)}"
            logger.error(f"[Verification] Missing indexes: {missing_indexes}")

        # Only proceed to data write operations if table exists
        if report["table_exists"]:
            session = SessionPostgres()
            test_url = f"https://verification-test-url-{uuid.uuid4()}.com"
            test_crawl_id = uuid.uuid4()
            
            # 5. Insert Test Record
            logger.info(f"[Verification] Inserting test record with URL {test_url}...")
            test_article = ScrapedNews(
                keyword="verification_test_keyword",
                title="Verification Test Title",
                url=test_url,
                source="VerificationSource",
                author="VerificationAuthor",
                published_date=datetime.now(timezone.utc),
                scraped_date=datetime.now(timezone.utc),
                language="English",
                country="Global",
                summary="Test summary",
                content="Test content",
                sentiment="neutral",
                category="Verification",
                crawl_id=test_crawl_id,
                status="verification_test"
            )
            session.add(test_article)
            session.commit()
            report["insert_record"] = True
            logger.info("[Verification] Test record inserted successfully.")
            
            # 6. Read Test Record
            logger.info("[Verification] Reading back inserted record...")
            fetched = session.scalars(select(ScrapedNews).where(ScrapedNews.url == test_url)).first()
            if fetched and fetched.title == "Verification Test Title":
                report["read_record"] = True
                logger.info("[Verification] Test record read back successfully and fields match.")
            else:
                report["details"]["read"] = "Could not retrieve test record or field mismatch."
                logger.error("[Verification] Failed to read back inserted record.")
                
            # 7. Delete Test Record
            logger.info("[Verification] Deleting test record...")
            session.delete(fetched)
            session.commit()
            
            # Verify deletion
            deleted_check = session.scalars(select(ScrapedNews).where(ScrapedNews.url == test_url)).first()
            if deleted_check is None:
                report["delete_record"] = True
                logger.info("[Verification] Test record deleted successfully.")
            else:
                report["details"]["delete"] = "Test record was not deleted."
                logger.error("[Verification] Failed to delete test record.")

    except Exception as e:
        report["details"]["error"] = str(e)
        logger.error(f"[Verification Exception] Database verification failed: {e}")
        if session:
            session.rollback()
    finally:
        if conn:
            conn.close()
        if session:
            session.close()

    # Determine overall status
    checks = [
        report["postgres_connection"],
        report["schema_exists"],
        report["table_exists"],
        report["indexes_exist"],
        report["insert_record"],
        report["read_record"],
        report["delete_record"]
    ]
    if all(checks):
        report["overall_status"] = "PASSED"
        logger.info("[Verification Status] OVERALL VERIFICATION PASSED.")
    else:
        report["overall_status"] = "FAILED"
        logger.error(f"[Verification Status] OVERALL VERIFICATION FAILED. Details: {report}")
        
    return report

if __name__ == "__main__":
    verify_integration()
