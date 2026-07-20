import os
import sys

# Adjust path to import backend modules
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import SessionLocal
from backend.queue_manager import process_search_query
from backend.models import SearchQuery
from sqlalchemy import select

def run_test():
    db = SessionLocal()
    try:
        # Find the pending SearchQuery we created in the previous test
        query = db.scalars(
            select(SearchQuery)
            .where(SearchQuery.status == "pending")
            .order_by(SearchQuery.id.desc())
            .limit(1)
        ).first()
        
        if not query:
            print("No pending query found. Creating one...")
            from test_schedule_crawl import run_test as create_one
            create_one()
            query = db.scalars(
                select(SearchQuery)
                .where(SearchQuery.status == "pending")
                .order_by(SearchQuery.id.desc())
                .limit(1)
            ).first()
            
        print(f"Running crawl for SearchQuery ID: {query.id}")
        process_search_query(query.id)
        
        # Reload query to check status
        db.refresh(query)
        print(f"Crawl completed with status: {query.status}")
        print(f"   Found: {query.total_urls_found}")
        print(f"   Crawled: {query.total_urls_crawled}")
        print(f"   Matched: {query.total_urls_matched}")
        if query.error_message:
            print(f"   Error: {query.error_message}")
            
    finally:
        db.close()

if __name__ == "__main__":
    run_test()
