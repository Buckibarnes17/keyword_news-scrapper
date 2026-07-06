#!/usr/bin/env python
"""
Keyword News Scraper - Search URL Inspector Utility
This script allows you to inspect search queries, analyze crawl statistics, and manage URL queues.
"""
import os
import sys
import argparse
from datetime import datetime

import os
import sys
import argparse
from datetime import datetime

# Force UTF-8 encoding for stdout/stderr to avoid UnicodeEncodeError in Windows terminals
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Add the current directory to sys.path so backend imports resolve correctly
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from dotenv import load_dotenv
from sqlalchemy import func
from backend.database import SessionLocal
from backend.models import SearchQuery, CrawledURL

load_dotenv()

# Utility to format terminal outputs beautifully
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def print_header(title):
    print("\n" + "=" * 80)
    print(f" {Colors.BOLD}{Colors.OKCYAN}{title.upper()}{Colors.ENDC}")
    print("=" * 80)

def print_success(message):
    print(f"{Colors.OKGREEN}[SUCCESS] {message}{Colors.ENDC}")

def print_warning(message):
    print(f"{Colors.WARNING}[WARNING] {message}{Colors.ENDC}")

def print_error(message):
    print(f"{Colors.FAIL}[ERROR] {message}{Colors.ENDC}")

def get_session():
    return SessionLocal()

def list_recent_searches(session, limit=15):
    """Lists recent search queries with summary stats."""
    print_header(f"Recent Search Queries (Last {limit})")
    
    queries = session.query(SearchQuery).order_by(SearchQuery.id.desc()).limit(limit).all()
    
    if not queries:
        print("No search queries found in the database.")
        return
        
    # Format data for nice column printing
    headers = ["ID", "Keyword", "Engine", "Status", "Found", "Crawled", "Matched", "Created At"]
    rows = []
    for q in queries:
        created_str = q.created_at.strftime("%Y-%m-%d %H:%M") if q.created_at else "N/A"
        # Color coding status
        status_color = q.status
        if q.status == "completed":
            status_color = f"{Colors.OKGREEN}{q.status}{Colors.ENDC}"
        elif q.status == "failed":
            status_color = f"{Colors.FAIL}{q.status}{Colors.ENDC}"
        elif q.status == "processing":
            status_color = f"{Colors.WARNING}{q.status}{Colors.ENDC}"
            
        rows.append([
            q.id,
            q.keyword[:25] + "..." if len(q.keyword) > 28 else q.keyword,
            q.engine,
            status_color,
            q.total_urls_found,
            q.total_urls_crawled,
            q.total_urls_matched,
            created_str
        ])
        
    try:
        import pandas as pd
        df = pd.DataFrame(rows, columns=headers)
        print(df.to_string(index=False))
    except ImportError:
        # Fallback to simple printing
        print(f"{'ID':<6} | {'Keyword':<30} | {'Engine':<8} | {'Status':<12} | {'Found':<6} | {'Crawled':<8} | {'Matched':<8} | {'Created At'}")
        print("-" * 100)
        for r in rows:
            print(f"{r[0]:<6} | {r[1]:<30} | {r[2]:<8} | {r[3]:<12} | {r[4]:<6} | {r[5]:<8} | {r[6]:<8} | {r[7]}")

def inspect_search_query(session, search_id):
    """Detailed inspection of a specific search query."""
    q = session.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    if not q:
        print_error(f"Search Query ID {search_id} not found.")
        return
        
    print_header(f"Inspection: Search Query #{search_id} - '{q.keyword}'")
    print(f"{Colors.BOLD}Keyword:{Colors.ENDC}         {q.keyword}")
    print(f"{Colors.BOLD}Status:{Colors.ENDC}          {q.status}")
    print(f"{Colors.BOLD}Engine:{Colors.ENDC}          {q.engine} (Source: {q.source_type})")
    print(f"{Colors.BOLD}Match Type:{Colors.ENDC}      {q.match_type} (Case Sensitive: {q.case_sensitive}, Exact Match: {q.exact_match})")
    if q.error_message:
        print(f"{Colors.BOLD}Error Message:{Colors.ENDC}   {Colors.FAIL}{q.error_message}{Colors.ENDC}")
    print(f"{Colors.BOLD}Created At:{Colors.ENDC}      {q.created_at}")
    print(f"{Colors.BOLD}Updated At:{Colors.ENDC}      {q.updated_at}")
    
    # Get stats of CrawledURLs
    crawled_stats = session.query(
        CrawledURL.status, 
        func.count(CrawledURL.id)
    ).filter(CrawledURL.search_id == search_id).group_by(CrawledURL.status).all()
    
    print("\n" + "-" * 40)
    print(f"{Colors.BOLD}CRAWLED URL STATUS BREAKDOWN{Colors.ENDC}")
    print("-" * 40)
    if not crawled_stats:
        print("No URLs crawled or discovered for this search query yet.")
    else:
        total = 0
        for status, count in crawled_stats:
            print(f" * {status:<12}: {count}")
            total += count
        print(f" * {'TOTAL':<12}: {total}")
        
    # Get error message breakdown if there are failed urls
    failed_counts = session.query(
        CrawledURL.error_message,
        func.count(CrawledURL.id)
    ).filter(CrawledURL.search_id == search_id, CrawledURL.status == 'failed').group_by(CrawledURL.error_message).all()
    
    if failed_counts:
        print("\n" + "-" * 40)
        print(f"{Colors.BOLD}FAILURE REASON BREAKDOWN{Colors.ENDC}")
        print("-" * 40)
        for err, count in failed_counts:
            err_msg = err if err else "Unknown Error"
            # Limit length of error message in summary
            err_msg = err_msg[:60] + "..." if len(err_msg) > 63 else err_msg
            print(f" * {count} url(s) failed with: {Colors.WARNING}{err_msg}{Colors.ENDC}")

def list_urls(session, search_id, status_filter=None, limit=10):
    """Lists URLs associated with a search ID, optionally filtered by status."""
    q = session.query(SearchQuery).filter(SearchQuery.id == search_id).first()
    if not q:
        print_error(f"Search Query ID {search_id} not found.")
        return
        
    query = session.query(CrawledURL).filter(CrawledURL.search_id == search_id)
    if status_filter:
        query = query.filter(CrawledURL.status == status_filter)
        print_header(f"URLs for Search #{search_id} (Status: {status_filter}, Limit: {limit})")
    else:
        print_header(f"URLs for Search #{search_id} (All Statuses, Limit: {limit})")
        
    urls = query.limit(limit).all()
    if not urls:
        print("No URLs found matching criteria.")
        return
        
    headers = ["ID", "Status", "Title", "Relevance", "Error/Info", "URL"]
    rows = []
    for u in urls:
        # Format title/error safely
        title_str = (u.title[:20] + "...") if u.title and len(u.title) > 23 else (u.title or "N/A")
        err_str = "N/A"
        if u.error_message:
            err_str = (u.error_message[:20] + "...") if len(u.error_message) > 23 else u.error_message
        elif u.matched_keywords:
            err_str = f"Keywords: {u.matched_keywords}"
            
        status_color = u.status
        if u.status == "matched":
            status_color = f"{Colors.OKGREEN}{u.status}{Colors.ENDC}"
        elif u.status == "failed":
            status_color = f"{Colors.FAIL}{u.status}{Colors.ENDC}"
        elif u.status == "skipped":
            status_color = f"{Colors.WARNING}{u.status}{Colors.ENDC}"
            
        rows.append([
            u.id,
            status_color,
            title_str,
            f"{u.relevance_score:.1f}" if u.relevance_score else "0.0",
            err_str,
            u.url[:60] + "..." if len(u.url) > 63 else u.url
        ])
        
    try:
        import pandas as pd
        df = pd.DataFrame(rows, columns=headers)
        print(df.to_string(index=False))
    except ImportError:
        print(f"{'ID':<6} | {'Status':<12} | {'Title':<23} | {'Relevance':<9} | {'Error/Info':<23} | {'URL'}")
        print("-" * 100)
        for r in rows:
            print(f"{r[0]:<6} | {r[1]:<12} | {r[2]:<23} | {r[3]:<9} | {r[4]:<23} | {r[5]}")

def export_urls(session, search_id, status_filter, output_path):
    """Exports URLs to a CSV file."""
    query = session.query(CrawledURL).filter(CrawledURL.search_id == search_id)
    if status_filter:
        query = query.filter(CrawledURL.status == status_filter)
        
    urls = query.all()
    if not urls:
        print_warning("No URLs to export.")
        return
        
    # Convert list of models to dictionary list
    data = []
    for u in urls:
        data.append({
            "id": u.id,
            "url": u.url,
            "domain": u.domain,
            "status": u.status,
            "title": u.title,
            "snippet": u.snippet,
            "relevance_score": u.relevance_score,
            "matched_keywords": u.matched_keywords,
            "error_message": u.error_message,
            "discovered_at": u.discovered_at
        })
        
    try:
        import pandas as pd
        df = pd.DataFrame(data)
        df.to_csv(output_path, index=False)
        print_success(f"Exported {len(urls)} URLs to {output_path}")
    except Exception as e:
        print_error(f"Failed to export data: {e}")

def reset_urls_status(session, search_id, source_status, target_status='pending'):
    """Resets URLs matching source_status back to target_status (e.g. pending) for retry."""
    print_warning(f"Preparing to reset URLs with status '{source_status}' to '{target_status}' for Search #{search_id}...")
    
    # Confirm count
    count = session.query(CrawledURL).filter(
        CrawledURL.search_id == search_id,
        CrawledURL.status == source_status
    ).count()
    
    if count == 0:
        print_warning(f"No URLs found with status '{source_status}' for Search #{search_id}.")
        return
        
    ans = input(f"Are you sure you want to reset {count} URLs to '{target_status}'? (y/N): ")
    if ans.lower() not in ['y', 'yes']:
        print("Operation cancelled.")
        return
        
    try:
        session.query(CrawledURL).filter(
            CrawledURL.search_id == search_id,
            CrawledURL.status == source_status
        ).update(
            {CrawledURL.status: target_status, CrawledURL.error_message: None},
            synchronize_session=False
        )
        session.commit()
        print_success(f"Successfully reset {count} URLs to '{target_status}'.")
    except Exception as e:
        session.rollback()
        print_error(f"Error resetting URLs: {e}")

def run_interactive(session):
    """Runs interactive menu mode."""
    while True:
        print_header("Keyword News Scraper - Database Inspection Utility")
        print(" 1. List recent search queries")
        print(" 2. Detailed inspection of a specific search query")
        print(" 3. List crawled URLs for a search query")
        print(" 4. Reset failed URLs back to pending (for retrying)")
        print(" 5. Reset skipped URLs back to pending")
        print(" 6. Exit")
        
        try:
            choice = input("\nSelect an option (1-6): ").strip()
            if choice == '1':
                limit_input = input("Enter limit (default 15): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 15
                list_recent_searches(session, limit)
            elif choice == '2':
                search_id = input("Enter Search Query ID: ").strip()
                if search_id.isdigit():
                    inspect_search_query(session, int(search_id))
                else:
                    print_error("Invalid Search Query ID.")
            elif choice == '3':
                search_id = input("Enter Search Query ID: ").strip()
                if not search_id.isdigit():
                    print_error("Invalid Search Query ID.")
                    continue
                
                status_filter = input("Filter by status (pending/crawling/matched/skipped/failed or leave empty for all): ").strip().lower()
                if status_filter not in ['pending', 'crawling', 'matched', 'skipped', 'failed', '']:
                    print_error("Invalid status filter.")
                    continue
                    
                limit_input = input("Enter limit (default 10): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 10
                
                list_urls(session, int(search_id), status_filter if status_filter else None, limit)
            elif choice == '4':
                search_id = input("Enter Search Query ID: ").strip()
                if search_id.isdigit():
                    reset_urls_status(session, int(search_id), 'failed', 'pending')
                else:
                    print_error("Invalid Search Query ID.")
            elif choice == '5':
                search_id = input("Enter Search Query ID: ").strip()
                if search_id.isdigit():
                    reset_urls_status(session, int(search_id), 'skipped', 'pending')
                else:
                    print_error("Invalid Search Query ID.")
            elif choice == '6':
                print("Exiting inspector utility. Goodbye!")
                break
            else:
                print_error("Invalid choice. Please select 1-6.")
        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print_error(f"An unexpected error occurred: {e}")
            
        input("\nPress Enter to continue...")

def main():
    parser = argparse.ArgumentParser(description="Inspect search queries and crawled URLs in the Keyword Scraper database.")
    parser.add_argument("--list", action="store_true", help="List recent search queries.")
    parser.add_argument("--search-id", type=int, help="Inspect a specific search query.")
    parser.add_argument("--status", type=str, choices=['pending', 'crawling', 'matched', 'skipped', 'failed'], help="Filter URLs by status.")
    parser.add_argument("--limit", type=int, default=10, help="Limit number of output records (default: 10).")
    parser.add_argument("--export", type=str, help="Export filtered URLs to a CSV file (specify path).")
    parser.add_argument("--reset-failed", action="store_true", help="Reset failed URLs of --search-id back to 'pending'.")
    parser.add_argument("--reset-skipped", action="store_true", help="Reset skipped URLs of --search-id back to 'pending'.")
    parser.add_argument("--interactive", "-i", action="store_true", help="Force interactive mode.")
    
    args = parser.parse_args()
    
    session = get_session()
    try:
        # Check if any standard flags were passed
        has_flags = args.list or args.search_id is not None or args.interactive
        
        if not has_flags:
            # If no args are passed, check if stdin/stdout are a tty to run interactively,
            # otherwise print recent searches as a fallback.
            if sys.stdin.isatty():
                run_interactive(session)
            else:
                list_recent_searches(session, args.limit)
        elif args.interactive:
            run_interactive(session)
        elif args.list:
            list_recent_searches(session, args.limit)
        elif args.search_id is not None:
            if args.reset_failed:
                reset_urls_status(session, args.search_id, 'failed', 'pending')
            elif args.reset_skipped:
                reset_urls_status(session, args.search_id, 'skipped', 'pending')
            elif args.export:
                export_urls(session, args.search_id, args.status, args.export)
            else:
                inspect_search_query(session, args.search_id)
                list_urls(session, args.search_id, args.status, args.limit)
    finally:
        session.close()

if __name__ == "__main__":
    main()

