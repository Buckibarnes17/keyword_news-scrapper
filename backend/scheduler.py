# ## Changes
# - Renamed stop event to _scheduler_stop_event to prevent collision with queue manager.
# - Replaced datetime.utcnow() with datetime.now(timezone.utc).
# - Configured triggered SearchQuery to inherit ignore_robots setting from schedule configuration.

import time
import json
import threading
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from backend.database import SessionLocal
from backend.models import SearchSchedule, SearchQuery

_scheduler_thread = None
_scheduler_stop_event = threading.Event()

def calculate_next_run(frequency: str, config: dict, base_time: datetime) -> datetime:
    """
    Calculates the next execution datetime strictly after base_time.
    Uses UTC parameters from config if available (schedule_time_hour, schedule_time_minute, etc.)
    to align with user-intended time.
    """
    import calendar
    # Ensure base_time is UTC timezone-aware
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)
        
    hour = config.get("schedule_time_hour")
    minute = config.get("schedule_time_minute")
    
    # If the config doesn't have the timing settings (fallback to old schedules)
    if hour is None or minute is None:
        if frequency == "daily":
            return base_time + timedelta(days=1)
        elif frequency == "weekly":
            return base_time + timedelta(weeks=1)
        elif frequency == "monthly":
            return base_time + timedelta(days=30)
        return base_time + timedelta(days=1)

    # We have custom time. Let's align base_time to target hour/minute
    target_time_today = base_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    if frequency == "daily":
        if target_time_today > base_time:
            return target_time_today
        return target_time_today + timedelta(days=1)
        
    elif frequency == "weekly":
        weekday = config.get("schedule_time_weekday") # 0=Sunday, 1=Monday ... 6=Saturday
        if weekday is None:
            if target_time_today > base_time:
                return target_time_today
            return target_time_today + timedelta(days=7)
            
        current_js_weekday = (base_time.weekday() + 1) % 7
        days_to_add = (weekday - current_js_weekday + 7) % 7
        candidate = target_time_today + timedelta(days=days_to_add)
        if candidate > base_time:
            return candidate
        return candidate + timedelta(days=7)
        
    elif frequency == "monthly":
        day = config.get("schedule_time_day")
        if day is None:
            day = 1
            
        year = base_time.year
        month = base_time.month
        max_days = calendar.monthrange(year, month)[1]
        actual_day = min(day, max_days)
        
        candidate = base_time.replace(year=year, month=month, day=actual_day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > base_time:
            return candidate
            
        month += 1
        if month > 12:
            month = 1
            year += 1
        max_days = calendar.monthrange(year, month)[1]
        actual_day = min(day, max_days)
        return base_time.replace(year=year, month=month, day=actual_day, hour=hour, minute=minute, second=0, microsecond=0)
        
    return base_time + timedelta(days=1)

def trigger_scheduled_search(db: Session, schedule: SearchSchedule):
    """
    Creates a new pending SearchQuery based on schedule parameters.
    """
    try:
        config = json.loads(schedule.config_json)
    except Exception as e:
        print(f"Error reading configuration for schedule {schedule.id}: {e}")
        return

    # Create new search query in pending state
    new_query = SearchQuery(
        keyword=schedule.keyword,
        match_type=config.get("match_type", "phrase"),
        case_sensitive=config.get("case_sensitive", False),
        exact_match=config.get("exact_match", False),
        domains_filter=json.dumps(config.get("domains_filter")) if config.get("domains_filter") else None,
        languages_filter=json.dumps(config.get("languages_filter")) if config.get("languages_filter") else None,
        engine=schedule.engine,
        source_type=config.get("source_type", "search"),
        direct_urls=config.get("direct_urls"),
        ignore_robots=config.get("ignore_robots", False),
        status="pending"
    )
    
    db.add(new_query)
    
    # Calculate next execution time relative to the expected next_run to prevent drift
    expected_run = schedule.next_run
    now = datetime.now(timezone.utc)
    schedule.last_run = now
    
    if expected_run.tzinfo is None:
        expected_run = expected_run.replace(tzinfo=timezone.utc)
        
    # If expected_run is in the past by more than 12 hours (e.g. server was offline),
    # reset reference run base to now to prevent back-to-back run loops
    if expected_run < now - timedelta(hours=12):
        reference_time = now
    else:
        reference_time = expected_run

    schedule.next_run = calculate_next_run(schedule.frequency, config, reference_time)
        
    db.commit()
    print(f"Triggered scheduled search query for keyword: '{schedule.keyword}' (Schedule ID: {schedule.id})")

def scheduler_loop():
    """Background loop checking and triggering active schedules."""
    while not _scheduler_stop_event.is_set():
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            # Find active schedules that are past due
            due_schedules = db.query(SearchSchedule).filter(
                SearchSchedule.active == True,
                SearchSchedule.next_run <= now
            ).all()
            
            for schedule in due_schedules:
                trigger_scheduled_search(db, schedule)
                
        except Exception as e:
            print(f"Error in scheduler loop: {e}")
        finally:
            db.close()
            
        time.sleep(10.0)  # Check schedules every 10 seconds

def start_scheduler():
    """Starts the scheduler background thread."""
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(target=scheduler_loop, name="SchedulerThread", daemon=True)
        _scheduler_thread.start()
        print("Scheduler Thread started successfully.")

def stop_scheduler():
    """Stops the scheduler background thread."""
    global _scheduler_thread
    _scheduler_stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=10)
        _scheduler_thread = None
        print("Scheduler Thread stopped.")
