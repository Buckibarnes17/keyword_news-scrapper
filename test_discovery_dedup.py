import os
import sys
import json
from datetime import datetime, timezone
import unittest
from unittest.mock import patch, MagicMock

# Adjust path to import backend modules
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import SessionLocal, init_db
from backend.models import SearchQuery, CrawledURL, KeywordProgress, SearchSchedule
from backend.queue_manager import process_search_query, run_direct_discovery
from backend.scheduler import trigger_scheduled_search
from backend.discovery.base import DiscoveredURL
from sqlalchemy import select, func

class TestDiscoveryDedup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    @patch('backend.queue_manager._load_site_profiles', return_value={})
    @patch('backend.queue_manager.Crawler')
    def test_run_direct_discovery_dedup_and_routing(self, mock_crawler_class, mock_profiles):
        # Setup mock Crawler behavior
        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        
        # HTML with some links to the same domain to trigger link expansion
        mock_crawler.fetch_page.return_value = """
        <html>
            <body>
                <a href="/news/1">News 1</a>
                <a href="/news/2">News 2</a>
                <a href="https://other.com/news">External</a>
            </body>
        </html>
        """

        # Set of test URLs: one sitemap, one feed, one regular
        urls = [
            "https://example.com/sitemap.xml",
            "https://example.com/rss",
            "https://example.com/regular-page"
        ]

        query_mock = MagicMock()
        query_mock.proxy_url = None
        query_mock.ignore_robots = True

        # Mock sitemap & feed discovery functions to count how many times they are called
        with patch('backend.sitemap_discovery.discover_from_sitemap') as mock_sitemap, \
             patch('backend.sitemap_discovery.discover_from_feeds') as mock_feeds:
            
            mock_sitemap.return_value = ["https://example.com/sitemap-item-1", "https://example.com/sitemap-item-2"]
            mock_feeds.return_value = ["https://example.com/feed-item-1"]

            # Run discovery
            candidates, domains = run_direct_discovery(urls, query_mock)

            # Assertions
            mock_sitemap.assert_called_once_with("https://example.com/sitemap.xml", max_urls=500)
            mock_feeds.assert_called_once_with("https://example.com/rss", max_urls=200)
            
            # The regular page should have fetched its page once for link expansion
            mock_crawler.fetch_page.assert_called_once_with(
                "https://example.com/regular-page", engine="fast", ignore_robots=True
            )

            # Verify resolved candidate URLs mapping - run_direct_discovery now returns
            # Dict[str, List[DiscoveredURL]], not plain strings, so the metadata that
            # DiscoveredURL carries survives this boundary. Unwrap .url for comparison.
            def _urls(key):
                return [du.url for du in candidates[key]]

            self.assertTrue(all(isinstance(du, DiscoveredURL) for du in candidates["https://example.com/sitemap.xml"]))
            self.assertEqual(_urls("https://example.com/sitemap.xml"), ["https://example.com/sitemap-item-1", "https://example.com/sitemap-item-2"])
            self.assertEqual(_urls("https://example.com/rss"), ["https://example.com/feed-item-1"])
            self.assertIn("https://example.com/news/1", _urls("https://example.com/regular-page"))
            self.assertIn("https://example.com/news/2", _urls("https://example.com/regular-page"))
            self.assertEqual(domains, {"example.com"})

    @patch('backend.queue_manager.Crawler')
    def test_process_search_query_runs_once_per_url(self, mock_crawler_class):
        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        # Just mock a basic page for both expansion and crawling
        mock_crawler.fetch_page.return_value = "<html><body><a href='/a'>a</a></body></html>"
        mock_crawler.analyze_page.return_value = {
            "matched": True,
            "language": "en",
            "content_hash": "dummy_hash",
            "discovered_at": datetime.now(timezone.utc)
        }

        # Create search query in DB
        # source_type = "direct", with multiple keywords and multiple URLs
        query = SearchQuery(
            keyword="china, myanmar",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            source_type="direct",
            direct_urls="https://example.com/page1\nhttps://example.com/page2",
            ignore_robots=True,
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        # Track how many times `fetch_page` is called during candidate discovery vs crawl phase
        # By patching run_direct_discovery, we can count it
        with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
            mock_disc.return_value = (
                {
                    "https://example.com/page1": [
                        DiscoveredURL(url="https://example.com/page1", source="legacy"),
                        DiscoveredURL(url="https://example.com/page1/a", source="legacy"),
                    ],
                    "https://example.com/page2": [
                        DiscoveredURL(url="https://example.com/page2", source="legacy"),
                    ]
                },
                {"example.com"}
            )

            # Process search query
            process_search_query(query.id)

            # Check that run_direct_discovery was called exactly once for the query
            mock_disc.assert_called_once()
            args, kwargs = mock_disc.call_args
            self.assertEqual(args[0], ["https://example.com/page1", "https://example.com/page2"])
            self.assertEqual(args[1].id, query.id)

            # Verify that query is completed and results exist
            self.db.refresh(query)
            self.assertEqual(query.status, "completed")
            
            # Check KeywordProgress records
            kps = self.db.scalars(select(KeywordProgress).where(KeywordProgress.search_query_id == query.id)).all()
            self.assertEqual(len(kps), 2)
            self.assertTrue(all(kp.status == "completed" for kp in kps))

if __name__ == "__main__":
    unittest.main()
