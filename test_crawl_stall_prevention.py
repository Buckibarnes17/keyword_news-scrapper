import os
import sys
import time
import unittest
from unittest.mock import patch
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# Ensure backend modules can be imported
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import SessionLocal, init_db
from backend.models import SearchQuery, CrawledURL, KeywordProgress
from backend.queue_manager import process_search_query, request_job_stop, is_job_stopped
from backend.crawler import Crawler

class MockServerRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logging to keep output clean

    def do_GET(self):
        if self.path == "/fast":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Fast page</h1><p>Contains Python keyword</p></body></html>")
        elif self.path == "/slow":
            # Sleep 60 seconds to simulate a hanging/stalled page load
            time.sleep(60)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Slow page</h1><p>Contains Python keyword</p></body></html>")
        elif self.path == "/sitemap.xml":
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            # Dynamic generation of URLs containing the local mock server's port
            host = self.headers.get("Host", "127.0.0.1")
            sitemap_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://{host}/fast</loc></url>
  <url><loc>http://{host}/slow</loc></url>
</urlset>"""
            self.wfile.write(sitemap_xml.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

class TestCrawlStallPrevention(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set database URL environment override to test SQLite file
        os.environ["DATABASE_URL"] = "sqlite:///test_stall.db"
        init_db()
        
        # Start threading server on ephemeral local port
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockServerRequestHandler)
        cls.port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        print(f"[Mock Server] Started local threading server on http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        # Clean up database file
        if os.path.exists("test_stall.db"):
            try:
                os.remove("test_stall.db")
            except Exception:
                pass

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    @patch('backend.crawler.SELENIUM_AVAILABLE', False)
    @patch('backend.postgres_integration.export_search_to_postgres')
    def test_crawler_fetch_page_http_timeout(self, mock_export):
        """Crawler fetch_page connects fast and times out read after 25s for slow HTTP requests."""
        crawler = Crawler()
        # Override retry policy to prevent urllib3 from retrying 3 times on timeout
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter
        no_retry_strategy = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0
        )
        crawler.session.mount("http://", HTTPAdapter(max_retries=no_retry_strategy))
        crawler.session.mount("https://", HTTPAdapter(max_retries=no_retry_strategy))

        start_time = time.time()
        url = f"http://127.0.0.1:{self.port}/slow"
        
        # Crawler's _fetch_http read timeout is 25s. It should raise an exception before 60s.
        with self.assertRaises(Exception):
            crawler.fetch_page(url, engine="fast", ignore_robots=True)
        
        duration = time.time() - start_time
        print(f"   [Test] fetch_page HTTP slow timeout took {duration:.2f} seconds")
        self.assertLess(duration, 35.0, "Should timeout fast and not block for 60s")
        crawler.close()

    @patch('backend.crawler.SELENIUM_AVAILABLE', False)
    @patch('backend.postgres_integration.export_search_to_postgres')
    def test_queue_manager_watchdog_and_progress(self, mock_export):
        """
        Main crawl loop watchdog terminates tasks taking >45s, and progress
        crawled-pages counter is updated incrementally in real time.
        """
        # Create SearchQuery task pointing to sitemap containing /fast and /slow
        query = SearchQuery(
            keyword="Python",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            source_type="sitemap",
            direct_urls=f"http://127.0.0.1:{self.port}/sitemap.xml",
            ignore_robots=True,
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        # Run process_search_query in a background thread so we can verify incremental progress updates
        t = threading.Thread(target=process_search_query, args=(query.id,))
        t.start()

        # Wait a few seconds for /fast URL crawl to complete and confirm incremental database update
        time.sleep(5.0)
        
        # Query database to confirm progress counter has updated incrementally before the slow page finishes
        self.db.commit() # Synchronize SQLAlchemy session
        self.db.refresh(query)
        print(f"   [Test Progress Check] Crawled URLs: {query.total_urls_crawled}/{query.total_urls_found}")
        
        # It should have crawled at least 1 URL (/fast)
        self.assertGreaterEqual(query.total_urls_crawled, 1, "Progress counter must increment in near real-time")
        
        # Join thread and wait for the rest to finish (watchdog should abandon /slow after 45s)
        t.join(timeout=60.0)
        self.assertFalse(t.is_alive(), "Crawl task did not complete within the 60s timeout limit")

        self.db.commit() # Synchronize SQLAlchemy session
        self.db.refresh(query)
        print(f"   [Test Watchdog Check] Completed run. Crawled: {query.total_urls_crawled}, Matched: {query.total_urls_matched}")
        self.assertEqual(query.total_urls_crawled, 2, "Both fast and slow tasks should be accounted for")
        self.assertEqual(query.status, "completed")

        # Verify slow page is marked failed in database
        slow_url_record = self.db.scalars(
            self.db.query(CrawledURL).filter(CrawledURL.search_id == query.id, CrawledURL.url.contains("/slow"))
        ).first()
        self.assertIsNotNone(slow_url_record)
        self.assertEqual(slow_url_record.status, "failed")
        self.assertIn("exceeded maximum duration", slow_url_record.error_message)

    @patch('backend.crawler.SELENIUM_AVAILABLE', False)
    @patch('backend.postgres_integration.export_search_to_postgres')
    def test_crawl_abort_sequence(self, mock_export):
        """Abort Crawl action terminates in-flight queue manager processing within a few seconds."""
        query = SearchQuery(
            keyword="Python",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            source_type="direct",
            direct_urls=f"http://127.0.0.1:{self.port}/slow\nhttp://127.0.0.1:{self.port}/fast",
            ignore_robots=True,
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        # Run process_search_query in a background thread
        t = threading.Thread(target=process_search_query, args=(query.id,))
        t.start()

        # Allow crawl to start, then request a prompt abort
        time.sleep(2.0)
        print("   [Test] Triggering abort crawl...")
        request_job_stop(query.id)

        # The thread should exit almost immediately (within a few seconds)
        start_abort = time.time()
        t.join(timeout=10.0)
        abort_duration = time.time() - start_abort

        print(f"   [Test] Abort sequence finished in {abort_duration:.2f} seconds")
        self.assertFalse(t.is_alive(), "Queue manager did not exit promptly on abort")
        self.assertLess(abort_duration, 5.0, "Abort must terminate within 5 seconds")

        self.db.commit()
        self.db.refresh(query)
        self.assertEqual(query.status, "aborted", "Query status must be updated to aborted")

if __name__ == "__main__":
    unittest.main()
