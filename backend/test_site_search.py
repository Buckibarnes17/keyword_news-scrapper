import unittest
from unittest.mock import MagicMock, patch
from bs4 import BeautifulSoup
import urllib.parse
import sys
import os

# Adjust path to import from backend
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.site_search_detector import (
    _detect_search_form,
    _build_search_url,
    _extract_result_urls,
    SiteSearchDetector,
    _is_search_url
)
from backend.url_classifier import is_search_result_page, filter_candidate_urls
from backend.crawler import Crawler
from backend.queue_manager import process_search_query
from backend.models import SearchQuery, CrawledURL, KeywordProgress
from backend.database import SessionLocal, init_db

class TestSiteSearch(unittest.TestCase):

    def test_detection_positive_search_type(self):
        # 1. Detection positive — standard <input type="search">
        html = '<form action="/search"><input type="search" name="q"><input type="hidden" name="site_id" value="42"></form>'
        soup = BeautifulSoup(html, "html.parser")
        result = _detect_search_form(soup)
        self.assertIsNotNone(result)
        action, name, hidden = result
        self.assertEqual(action, "/search")
        self.assertEqual(name, "q")
        self.assertEqual(hidden, {"site_id": "42"})

    def test_detection_positive_search_named_text(self):
        # 2. Detection positive — form with search-named text input
        html = '<form action="/find"><input type="text" name="query" placeholder="Search..."></form>'
        soup = BeautifulSoup(html, "html.parser")
        result = _detect_search_form(soup)
        self.assertIsNotNone(result)
        action, name, hidden = result
        self.assertEqual(action, "/find")
        self.assertEqual(name, "query")
        self.assertEqual(hidden, {})

    def test_detection_negative_login(self):
        # 3. Detection negative — login form
        html = '<form action="/login"><input type="text" name="username"><input type="password" name="password"></form>'
        soup = BeautifulSoup(html, "html.parser")
        result = _detect_search_form(soup)
        self.assertIsNone(result)

    def test_detection_negative_no_form(self):
        # 4. Detection negative — no form
        html = '<html><body><p>Hello world</p></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        result = _detect_search_form(soup)
        self.assertIsNone(result)

    def test_build_search_url(self):
        # 5. _build_search_url correctness
        url = _build_search_url(
            action="/search",
            input_name="q",
            keyword="machine learning",
            hidden_fields={"lang": "en"},
            base_url="https://example.com"
        )
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "example.com")
        self.assertEqual(parsed.path, "/search")
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(params.get("q"), ["machine learning"])
        self.assertEqual(params.get("lang"), ["en"])

    def test_extract_result_urls_same_domain_filtering(self):
        # 6. _extract_result_urls same-domain filtering
        # Build a mock search results page with 5 same-domain links and 3 external links plus 2 static asset links.
        html = """
        <html>
          <body>
            <!-- Same-domain content links -->
            <a href="/article1">Article 1</a>
            <a href="https://example.com/article2">Article 2</a>
            <a href="/section/article3">Article 3</a>
            <a href="/news/article4">Article 4</a>
            <a href="/info/article5">Article 5</a>
            
            <!-- External links -->
            <a href="https://google.com/xyz">Google</a>
            <a href="http://otherdomain.com/abc">Other</a>
            <a href="//external.com/efg">External</a>
            
            <!-- Static assets -->
            <a href="/image.png">Image</a>
            <a href="/style.css">Stylesheet</a>
          </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        urls, next_page = _extract_result_urls(soup, "https://example.com/search?q=test", "example.com")
        
        self.assertEqual(len(urls), 5)
        self.assertIn("https://example.com/article1", urls)
        self.assertIn("https://example.com/article2", urls)
        self.assertIn("https://example.com/section/article3", urls)
        self.assertIn("https://example.com/news/article4", urls)
        self.assertIn("https://example.com/info/article5", urls)
        
        # Verify no external or static assets are included
        for url in urls:
            self.assertTrue("example.com" in url)
            self.assertFalse(url.endswith(".png"))
            self.assertFalse(url.endswith(".css"))

    def test_discover_fallthrough_on_no_form(self):
        # 7. SiteSearchDetector.discover() fallthrough on no form
        crawler = MagicMock()
        crawler.fetch_page.return_value = "<html><body>No search here</body></html>"
        detector = SiteSearchDetector(crawler)
        result = detector.discover("https://example.com", "keyword")
        self.assertIsNone(result)

    def test_discover_fallthrough_on_fetch_error(self):
        # 8. SiteSearchDetector.discover() fallthrough on fetch error
        crawler = MagicMock()
        crawler.fetch_page.side_effect = Exception("Connection error")
        detector = SiteSearchDetector(crawler)
        result = detector.discover("https://example.com", "keyword")
        self.assertIsNone(result)

    def test_integration_process_search_query_fallthrough(self):
        # 9. Integration: process_search_query fallthrough path unchanged
        init_db()
        db = SessionLocal()
        query = None
        try:
            # Create a mock SearchQuery record for "direct"
            query = SearchQuery(
                source_type="direct",
                keyword="testkeyword",
                direct_urls="https://example.com/nofile",
                status="pending",
                engine="fast",
                ignore_robots=True
            )
            db.add(query)
            db.commit()
            db.refresh(query)
            
            # Mock Crawler's fetch_page to return a page without search form but with normal link expansion
            with patch('backend.queue_manager.Crawler') as mock_crawler_class, \
                 patch('backend.queue_manager.search_web') as mock_search_web:
                mock_crawler = MagicMock()
                mock_crawler_class.return_value = mock_crawler
                mock_search_web.return_value = []
                
                # Fetching direct url returns some link-expansion candidate links
                mock_crawler.fetch_page.return_value = """
                <html>
                  <body>
                    <a href="/expanded1">Link 1</a>
                    <a href="/expanded2">Link 2</a>
                  </body>
                </html>
                """
                
                # Run the query
                process_search_query(query.id)
                
                # Verify that it expanded links normally
                db.refresh(query)
                crawled_urls = db.query(CrawledURL).filter(CrawledURL.search_id == query.id).all()
                urls = [c.url for c in crawled_urls]
                
                # Should contain original URL + expanded links
                self.assertIn("https://example.com/nofile", urls)
                self.assertIn("https://example.com/expanded1", urls)
                self.assertIn("https://example.com/expanded2", urls)
        finally:
            if query:
                # Cleanup database records
                db.query(CrawledURL).filter(CrawledURL.search_id == query.id).delete()
                db.query(KeywordProgress).filter(KeywordProgress.search_query_id == query.id).delete()
                db.delete(query)
                db.commit()
            db.close()

    def test_integration_site_search_skips_link_expansion(self):
        # 10. Integration: site search path skips link-expansion
        init_db()
        db = SessionLocal()
        query = None
        try:
            query = SearchQuery(
                source_type="direct",
                keyword="python",
                direct_urls="https://example.com/searchable",
                status="pending",
                engine="fast",
                ignore_robots=True
            )
            db.add(query)
            db.commit()
            db.refresh(query)
            
            with patch('backend.queue_manager.Crawler') as mock_crawler_class, \
                 patch('backend.queue_manager.search_web') as mock_search_web:
                mock_crawler = MagicMock()
                mock_crawler_class.return_value = mock_crawler
                mock_search_web.return_value = []
                
                # First fetch (detection on direct URL) -> contains a search form
                # Second fetch (the search result page itself) -> contains search results
                detection_html = """
                <html>
                  <body>
                    <form action="/search">
                      <input type="search" name="q">
                    </form>
                    <!-- These normal page links should NOT be expanded because we intercept -->
                    <a href="/should-not-expand">Should Not Expand</a>
                  </body>
                </html>
                """
                results_html = """
                <html>
                  <body>
                    <a href="/result1">Result 1</a>
                    <a href="/result2">Result 2</a>
                  </body>
                </html>
                """
                
                def fetch_page_side_effect(url, **kwargs):
                    if "q=" in url:
                        return results_html
                    return detection_html
                    
                mock_crawler.fetch_page.side_effect = fetch_page_side_effect
                
                # Run query
                process_search_query(query.id)
                
                # Verify crawled URLs
                db.refresh(query)
                crawled_urls = db.query(CrawledURL).filter(CrawledURL.search_id == query.id).all()
                urls = [c.url for c in crawled_urls]
                
                # Should contain original + results, but NOT /should-not-expand
                self.assertIn("https://example.com/searchable", urls)
                self.assertIn("https://example.com/result1", urls)
                self.assertIn("https://example.com/result2", urls)
                self.assertNotIn("https://example.com/should-not-expand", urls)
        finally:
            if query:
                # Cleanup database records
                db.query(CrawledURL).filter(CrawledURL.search_id == query.id).delete()
                db.query(KeywordProgress).filter(KeywordProgress.search_query_id == query.id).delete()
                db.delete(query)
                db.commit()
            db.close()

    def test_is_search_url_site_search_detector(self):
        # Unit tests for _is_search_url() in site_search_detector.py
        self.assertTrue(_is_search_url("https://www.aspi.org.au/page/2/?s=IOSI+Global")) # WP paginated search
        self.assertTrue(_is_search_url("https://site.com/search?q=test"))                 # search path
        self.assertTrue(_is_search_url("https://site.com/?q=test"))                       # search query param
        self.assertFalse(_is_search_url("https://thehindu.com/news/page/2/"))             # pagination, no search
        self.assertFalse(_is_search_url("https://aspi.org.au/report/iosi-global"))        # clean article URL
        self.assertFalse(_is_search_url("https://example.com/research/article"))          # /research/ not /search/

    def test_url_classifier(self):
        # Unit tests for url_classifier.py
        # Must be FILTERED (True)
        self.assertTrue(is_search_result_page("http://usanasfoundation.com/search?q=IOSI+Global"))
        self.assertTrue(is_search_result_page("https://www.aspi.org.au/page/2/?s=IOSI+Global"))
        self.assertTrue(is_search_result_page("https://site.com/?s=cricket"))
        self.assertTrue(is_search_result_page("https://site.com/?q=balochistan"))
        self.assertTrue(is_search_result_page("https://site.com/search/"))
        self.assertTrue(is_search_result_page("https://site.com/results?query=nuclear"))
        self.assertTrue(is_search_result_page("https://site.com/find?keyword=test"))

        # Must PASS THROUGH (False) — these are legitimate content URLs
        self.assertFalse(is_search_result_page("https://aspi.org.au/report/iosi-global-report"))
        self.assertFalse(is_search_result_page("https://thehindu.com/news/national/article-123.html"))
        self.assertFalse(is_search_result_page("https://idsa.in/issuebrief/west-asia-transitions"))
        self.assertFalse(is_search_result_page("https://example.com/research/climate-study")) # /research/ != /search/
        self.assertFalse(is_search_result_page("https://example.com/findings/new-report"))    # /findings/ != /find/
        self.assertFalse(is_search_result_page("https://thehindu.com/news/page/2/"))          # pagination alone, no search
        self.assertFalse(is_search_result_page("https://site.com/page/2/"))                  # archive pagination
        self.assertFalse(is_search_result_page("https://site.com/article?id=123&ref=homepage")) # id/ref not search params

        # filter_candidate_urls integration test
        urls = [
            "http://usanasfoundation.com/search?q=IOSI+Global",    # REMOVE
            "https://www.aspi.org.au/page/2/?s=IOSI+Global",       # REMOVE
            "https://aspi.org.au/report/iosi-global-2024",          # KEEP
            "https://idsa.in/issuebrief/west-asia-transitions",     # KEEP
            "https://site.com/?s=cricket",                          # REMOVE
            "https://thehindu.com/news/national/article-123.html",  # KEEP
        ]
        result = filter_candidate_urls(urls)
        self.assertEqual(len(result), 3)
        self.assertIn("https://aspi.org.au/report/iosi-global-2024", result)
        self.assertIn("https://idsa.in/issuebrief/west-asia-transitions", result)
        self.assertIn("https://thehindu.com/news/national/article-123.html", result)

if __name__ == "__main__":
    unittest.main()
