import os
import sys

# Adjust path to import from backend
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import SessionLocal
from backend.models import SearchQuery, SearchSchedule
from sqlalchemy import select

def inspect():
    db = SessionLocal()
    try:
        print("=== Search Queries ===")
        queries = db.scalars(select(SearchQuery).order_by(SearchQuery.id.desc()).limit(15)).all()
        for q in queries:
            print(f"ID: {q.id}, Keyword: {q.keyword}, Source Type: {q.source_type}, Status: {q.status}")
            print(f"   Found: {q.total_urls_found}, Crawled: {q.total_urls_crawled}, Matched: {q.total_urls_matched}")
            if q.error_message:
                print(f"   Error: {q.error_message}")
            print(f"   Created At: {q.created_at}")
            print("-" * 50)
            
        print("\n=== Active Schedules ===")
        scheds = db.scalars(select(SearchSchedule)).all()
        for s in scheds:
            print(f"ID: {s.id}, Keyword: {s.keyword}, Freq: {s.frequency}, Active: {s.active}, Next Run: {s.next_run}")
            print(f"   Config: {s.config_json[:200]}...")
            print("-" * 50)
    finally:
        db.close()

if __name__ == "__main__":
    inspect()
