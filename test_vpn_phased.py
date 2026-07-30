import os
import sys
import json
from datetime import datetime, timezone
import unittest
from unittest.mock import patch, MagicMock, call
from sqlalchemy import select

# Set database URL to a file-based SQLite for schema persistence across connections
TEST_DB_PATH = "test_vpn.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

# Adjust path to import backend modules
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import SessionLocal, init_db
from backend.models import SearchQuery, CrawledURL, KeywordProgress
from backend.queue_manager import process_search_query
import backend.expressvpn_router as evpn

class TestVPNPhased(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Remove any leftover test database
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass
        init_db()

    @classmethod
    def tearDownClass(cls):
        # Clean up test database file after all tests finish
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def setUp(self):
        self.db = SessionLocal()
        # Reset cached normal IP
        evpn._normal_ip = None

        # Start mock patches for search_web and export_search_to_postgres to prevent network calls
        self.patch_search_web = patch('backend.queue_manager.search_web', return_value=[])
        self.patch_postgres = patch('backend.postgres_integration.export_search_to_postgres')
        self.mock_search_web = self.patch_search_web.start()
        self.mock_postgres = self.patch_postgres.start()

    def tearDown(self):
        # Stop mock patches
        self.patch_search_web.stop()
        self.patch_postgres.stop()

        # Ensure any leftover lock is released
        if evpn.vpn_lock.locked():
            evpn.vpn_lock.release()
        self.db.close()

    @patch('backend.expressvpn_router.run_expressvpn_cmd')
    @patch('backend.expressvpn_router.get_current_ip_info')
    @patch('backend.queue_manager.Crawler')
    def test_mixed_job_phased_flow(self, mock_crawler_class, mock_ip_info, mock_vpn_cmd):
        """Mixed job: Connect -> Verify -> Crawl Chinese -> Disconnect -> Verify -> Crawl Normal"""
        # 1. Setup Mock VPN state and Geolocations
        # First check (Disconnected) -> connect -> status (Connected) -> verify_singapore -> disconnect -> verify_normal
        mock_vpn_cmd.side_effect = [
            (0, "Disconnected", ""), # get_connection_state check 1
            (0, "", ""),             # connect "singapore-cbd"
            (0, "Connected", ""),    # get_connection_state check 2
            (0, "", ""),             # disconnect
            (0, "Disconnected", ""), # get_connection_state check 3
        ]
        
        mock_ip_info.side_effect = [
            {"ip": "1.1.1.1", "country_code": "IN"}, # Cache normal IP
            {"ip": "2.2.2.2", "country_code": "SG"}, # Singapore verify
            {"ip": "1.1.1.1", "country_code": "IN"}, # Normal revert verify
        ]

        # Mock Crawler behavior
        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        mock_crawler.fetch_page.return_value = "<html><body>Match text</body></html>"
        def side_effect_analyze(html_content, url, keyword, **kwargs):
            return {
                "matched": True,
                "language": "en",
                "content_hash": f"hash_{url}",
                "discovered_at": datetime.now(timezone.utc)
            }
        mock_crawler.analyze_page.side_effect = side_effect_analyze

        # Create SearchQuery in DB
        query = SearchQuery(
            keyword="security",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            source_type="direct",
            direct_urls="https://www.chinadaily.com.cn/\nhttps://dawn.com/",
            ignore_robots=True,
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        # Mock direct discovery to return both Chinese and Normal URLs
        with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
            mock_disc.return_value = (
                {
                    "https://www.chinadaily.com.cn/": ["https://www.chinadaily.com.cn/"],
                    "https://dawn.com/": ["https://dawn.com/"]
                },
                {"chinadaily.com.cn", "dawn.com"}
            )

            # Process
            process_search_query(query.id)

        self.db.refresh(query)

        # Assert query finished successfully
        self.assertEqual(query.status, "completed")
        self.assertIsNone(query.status_message)

        # Check that both URLs were crawled
        crawled_urls = self.db.scalars(select(CrawledURL).where(CrawledURL.search_id == query.id)).all()
        self.assertEqual(len(crawled_urls), 2)
        
        statuses = {c.url: c.status for c in crawled_urls}
        self.assertEqual(statuses["https://www.chinadaily.com.cn/"], "matched")
        self.assertEqual(statuses["https://dawn.com/"], "matched")

        # Verify VPN command execution sequencing
        mock_vpn_cmd.assert_any_call(["connect", "singapore-cbd"])
        mock_vpn_cmd.assert_any_call(["disconnect"])

    @patch('backend.expressvpn_router.run_expressvpn_cmd')
    @patch('backend.expressvpn_router.get_current_ip_info')
    @patch('backend.queue_manager.Crawler')
    def test_only_chinese_skips_normal(self, mock_crawler_class, mock_ip_info, mock_vpn_cmd):
        """Only Chinese sources: VPN connects and disconnects, normal phase skipped."""
        mock_vpn_cmd.side_effect = [
            (0, "Disconnected", ""), # get_connection_state check
            (0, "", ""),             # connect
            (0, "Connected", ""),    # get_connection_state check
            (0, "", ""),             # disconnect
            (0, "Disconnected", ""), # get_connection_state check
        ]
        
        mock_ip_info.side_effect = [
            {"ip": "1.1.1.1", "country_code": "IN"}, # Cache
            {"ip": "2.2.2.2", "country_code": "SG"}, # SG verify
            {"ip": "1.1.1.1", "country_code": "IN"}, # Normal verify
        ]

        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        mock_crawler.fetch_page.return_value = "<html><body>Match text</body></html>"
        mock_crawler.analyze_page.return_value = {
            "matched": True,
            "language": "en",
            "content_hash": "hash_456",
            "discovered_at": datetime.now(timezone.utc)
        }

        query = SearchQuery(
            keyword="security",
            match_type="phrase",
            source_type="direct",
            direct_urls="https://www.chinadaily.com.cn/",
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
            mock_disc.return_value = (
                {"https://www.chinadaily.com.cn/": ["https://www.chinadaily.com.cn/"]},
                {"chinadaily.com.cn"}
            )
            process_search_query(query.id)

        self.db.refresh(query)
        self.assertEqual(query.status, "completed")
        mock_vpn_cmd.assert_any_call(["connect", "singapore-cbd"])
        mock_vpn_cmd.assert_any_call(["disconnect"])

    @patch('backend.expressvpn_router.run_expressvpn_cmd')
    @patch('backend.expressvpn_router.get_current_ip_info')
    @patch('backend.queue_manager.Crawler')
    def test_only_normal_skips_vpn(self, mock_crawler_class, mock_ip_info, mock_vpn_cmd):
        """Only normal sources: ExpressVPN never touched."""
        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        mock_crawler.fetch_page.return_value = "<html><body>Match text</body></html>"
        mock_crawler.analyze_page.return_value = {
            "matched": True,
            "language": "en",
            "content_hash": "hash_789",
            "discovered_at": datetime.now(timezone.utc)
        }

        query = SearchQuery(
            keyword="security",
            match_type="phrase",
            source_type="direct",
            direct_urls="https://dawn.com/",
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
            mock_disc.return_value = (
                {"https://dawn.com/": ["https://dawn.com/"]},
                {"dawn.com"}
            )
            process_search_query(query.id)

        self.db.refresh(query)
        self.assertEqual(query.status, "completed")
        mock_vpn_cmd.assert_not_called()
        mock_ip_info.assert_not_called()

    @patch('backend.expressvpn_router.run_expressvpn_cmd')
    @patch('backend.expressvpn_router.get_current_ip_info')
    @patch('backend.queue_manager.Crawler')
    def test_vpn_disabled_bypass(self, mock_crawler_class, mock_ip_info, mock_vpn_cmd):
        """If KS_DISABLE_VPN is true, Chinese sources are crawled in Normal phase without VPN."""
        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        mock_crawler.fetch_page.return_value = "<html><body>Match text</body></html>"
        mock_crawler.analyze_page.return_value = {
            "matched": True,
            "language": "en",
            "content_hash": "hash_disabled_vpn",
            "discovered_at": datetime.now(timezone.utc)
        }

        query = SearchQuery(
            keyword="security",
            match_type="phrase",
            source_type="direct",
            direct_urls="https://www.chinadaily.com.cn/",
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        # Set environment override to disable VPN
        import os
        os.environ["KS_DISABLE_VPN"] = "true"
        try:
            with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
                mock_disc.return_value = (
                    {"https://www.chinadaily.com.cn/": ["https://www.chinadaily.com.cn/"]},
                    {"chinadaily.com.cn"}
                )
                process_search_query(query.id)
        finally:
            # Restore to default/empty
            os.environ.pop("KS_DISABLE_VPN", None)

        self.db.refresh(query)
        self.assertEqual(query.status, "completed")
        # Ensure ExpressVPN was never called
        mock_vpn_cmd.assert_not_called()
        mock_ip_info.assert_not_called()

    @patch('backend.expressvpn_router.run_expressvpn_cmd')
    @patch('backend.expressvpn_router.get_current_ip_info')
    @patch('backend.queue_manager.Crawler')
    def test_vpn_connect_failure_safeguard(self, mock_crawler_class, mock_ip_info, mock_vpn_cmd):
        """VPN connect fails: abort Phase 1 (fail Chinese URLs), still run Phase 2 (crawl Normal)."""
        mock_vpn_cmd.side_effect = [
            (0, "Disconnected", ""), # get_connection_state check
            (1, "", "Daemon not running"), # connect command fails!
        ]
        
        mock_ip_info.side_effect = [
            {"ip": "1.1.1.1", "country_code": "IN"}, # Cache
        ]

        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        mock_crawler.fetch_page.return_value = "<html><body>Match text</body></html>"
        mock_crawler.analyze_page.return_value = {
            "matched": True,
            "language": "en",
            "content_hash": "hash_abc",
            "discovered_at": datetime.now(timezone.utc)
        }

        query = SearchQuery(
            keyword="security",
            match_type="phrase",
            source_type="direct",
            direct_urls="https://www.chinadaily.com.cn/\nhttps://dawn.com/",
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
            mock_disc.return_value = (
                {
                    "https://www.chinadaily.com.cn/": ["https://www.chinadaily.com.cn/"],
                    "https://dawn.com/": ["https://dawn.com/"]
                },
                {"chinadaily.com.cn", "dawn.com"}
            )
            process_search_query(query.id)

        self.db.refresh(query)
        # The overall search run should complete successfully (or completed because normal phase runs)
        self.assertEqual(query.status, "completed")

        crawled_urls = self.db.scalars(select(CrawledURL).where(CrawledURL.search_id == query.id)).all()
        statuses = {c.url: c.status for c in crawled_urls}
        err_msgs = {c.url: c.error_message for c in crawled_urls}

        # Chinese site should be marked as failed due to VPN routing failure
        self.assertEqual(statuses["https://www.chinadaily.com.cn/"], "failed")
        self.assertIn("VPN routing failure", err_msgs["https://www.chinadaily.com.cn/"])

        # Normal site should still be crawled successfully
        self.assertEqual(statuses["https://dawn.com/"], "matched")

    @patch('backend.queue_manager.Crawler')
    def test_no_keyword_article_and_date_filtering(self, mock_crawler_class):
        """Scrape without keyword: skip homepages, match articles under 3 months, skip older articles."""
        mock_crawler = MagicMock()
        mock_crawler_class.return_value = mock_crawler
        mock_crawler.fetch_page.return_value = "<html><body>Some article content</body></html>"
        
        # Mock analyze_page to return dates matching their URL structures
        def side_effect_analyze(html_content, url, keyword, **kwargs):
            from backend.queue_manager import extract_date_from_url
            pub_date = extract_date_from_url(url)
            return {
                "matched": True,
                "language": "en",
                "content_hash": f"hash_{url}",
                "discovered_at": pub_date,
                "domain": "chinadaily.com.cn"
            }
        mock_crawler.analyze_page.side_effect = side_effect_analyze

        # Create SearchQuery with no keyword option (keyword='__config__')
        query = SearchQuery(
            keyword="__config__",
            match_type="phrase",
            source_type="direct",
            direct_urls="https://www.chinadaily.com.cn/",
            status="pending"
        )
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)

        # Mock direct discovery to return homepage, category page, fresh article, and old article
        with patch('backend.queue_manager.run_direct_discovery') as mock_disc:
            mock_disc.return_value = (
                {
                    "https://www.chinadaily.com.cn/": [
                        "https://www.chinadaily.com.cn/",
                        "https://www.chinadaily.com.cn/world",
                        "https://www.chinadaily.com.cn/a/202607/27/WS66a4bc27a31095c1c3a1e1b1.html",
                        "https://www.chinadaily.com.cn/a/202401/01/WSold.html"
                    ]
                },
                {"chinadaily.com.cn"}
            )
            # Process search query (VPN is skipped in tests as we mock verify/check calls if any,
            # but here it is mixed/VPN since domain is Chinese.
            # Let's mock expressvpn_router connect/disconnect calls to avoid hitting routing locks)
            with patch('backend.expressvpn_router.connect_singapore'), \
                 patch('backend.expressvpn_router.verify_singapore_ip'), \
                 patch('backend.expressvpn_router.disconnect'), \
                 patch('backend.expressvpn_router.verify_normal_ip'):
                process_search_query(query.id)

        self.db.refresh(query)
        self.assertEqual(query.status, "completed")

        crawled_urls = self.db.scalars(select(CrawledURL).where(CrawledURL.search_id == query.id)).all()
        statuses = {c.url: c.status for c in crawled_urls}
        err_msgs = {c.url: c.error_message for c in crawled_urls}

        # 1. Homepage URL should be skipped immediately
        self.assertEqual(statuses["https://www.chinadaily.com.cn/"], "skipped")
        self.assertIn("Homepage or category/index landing page", err_msgs["https://www.chinadaily.com.cn/"])

        # 2. Category URL should be skipped immediately
        self.assertEqual(statuses["https://www.chinadaily.com.cn/world"], "skipped")
        self.assertIn("Homepage or category/index landing page", err_msgs["https://www.chinadaily.com.cn/world"])

        # 3. Fresh article URL (July 2026, within 3 months of July 2026 current time) should be matched
        self.assertEqual(statuses["https://www.chinadaily.com.cn/a/202607/27/WS66a4bc27a31095c1c3a1e1b1.html"], "matched")

        # 4. Old article URL (Jan 2024, > 3 months ago) should be skipped
        self.assertEqual(statuses["https://www.chinadaily.com.cn/a/202401/01/WSold.html"], "skipped")
        self.assertIn("Article published more than 3 months ago", err_msgs["https://www.chinadaily.com.cn/a/202401/01/WSold.html"])


if __name__ == "__main__":
    from sqlalchemy import select
    unittest.main()
