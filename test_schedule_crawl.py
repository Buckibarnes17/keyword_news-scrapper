import os
import sys
import json
from datetime import datetime, timezone

# Adjust path to import backend modules
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import SessionLocal, init_db
from backend.models import SearchQuery, SearchSchedule
from backend.scheduler import trigger_scheduled_search
from backend.queue_manager import process_search_query
from sqlalchemy import select

def run_test():
    init_db()
    db = SessionLocal()
    
    try:
        # Create a mock schedule with source_type='config'
        config_payload = {
            "keyword": "__config__",
            "match_type": "phrase",
            "case_sensitive": False,
            "exact_match": False,
            "engine": "fast",
            "source_type": "config",
            "direct_urls": None,
            "ignore_robots": True,
            "proxy_url": None
        }
        
        sched = SearchSchedule(
            keyword="__config__",
            frequency="daily",
            active=True,
            engine="fast",
            config_json=json.dumps(config_payload),
            next_run=datetime.now(timezone.utc)
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
        print(f"[SUCCESS] Created mock schedule with ID: {sched.id}")
        
        # Trigger the scheduled search
        print("Triggering scheduled search...")
        trigger_scheduled_search(db, sched)
        
        # Query the newly created SearchQuery
        query = db.scalars(
            select(SearchQuery)
            .order_by(SearchQuery.id.desc())
            .limit(1)
        ).first()
        
        if query:
            print(f"[SUCCESS] Triggered SearchQuery ID: {query.id}")
            print(f"   Keyword: {query.keyword[:100]}...")
            print(f"   Source Type: {query.source_type}")
            print(f"   Direct URLs length: {len(query.direct_urls) if query.direct_urls else 0}")
            
            # Assertions
            assert query.source_type == "direct" or query.source_type == "config", "Source type must be direct or config"
            assert len(query.direct_urls) > 0, "direct_urls must be resolved and non-empty"
            assert len(query.keyword) > 0, "keyword must be resolved and non-empty"
            print("[SUCCESS] Schedule trigger assertions passed.")
        else:
            print("[FAIL] No SearchQuery created by schedule trigger.")
            
    finally:
        db.close()

if __name__ == "__main__":
    run_test()
