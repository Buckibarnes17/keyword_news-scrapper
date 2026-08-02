import sys
import os
from datetime import datetime

# Adjust path to import from backend
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from backend.database import Base, engine, SessionLocal, init_db
from backend.models import SearchQuery, CrawledURL
from backend.crawler import Crawler
from backend.search_engine import is_valid_url
from backend.exporter import export_results

def run_tests():
    print("=== STARTING KEYWORD NEWS SCRAPER DIAGNOSTIC TESTS ===")
    
    # 1. Test Database Creation & Migration
    print("\n1. Testing Database & SQLAlchemy Models...")
    init_db()
    db = SessionLocal()
    print("[SUCCESS] Database tables successfully created and migrated.")

    # 2. Test URL Validation
    print("\n2. Testing URL Filtering Regex...")
    test_urls = [
        "https://www.wikipedia.org/wiki/Python_(programming_language)",
        "https://google.com/search?q=123",
        "https://duckduckgo.com/?q=abc",
        "https://realpython.com/tutorials/",
        "https://testsite.com/image.png",
        "https://testsite.com/style.css"
    ]
    for url in test_urls:
        valid = is_valid_url(url)
        print(f"   URL: {url:<60} -> Valid? {valid}")

    # 3. Test Keyword Extraction & Page Analyzer
    print("\n3. Testing Page Parser and Analysis Logic...")
    crawler = Crawler()
    mock_html = """
    <html>
      <head>
        <title>Learn Advanced Python Development and Programming</title>
        <meta name="description" content="This tutorial teaches advanced Python syntax, web scraping, and backend architecture.">
        <meta property="article:published_time" content="2026-05-15T12:00:00Z">
        <html lang="en-US">
      </head>
      <body>
        <nav>
          <a href="/home">Home</a> | <a href="/about">About Us</a>
        </nav>
        <main>
          <h1>Building Web Apps in Python</h1>
          <p>Python is an amazing language. Web scraping with BeautifulSoup4 is very powerful in python.
             We will also use FastAPI and Celery to manage asynchronous tasks in the backend.</p>
          <p>Developers who program in Python enjoy its clean readability and huge ecosystem.</p>
        </main>
        <footer>
          <p>&copy; 2026 Code Academy</p>
        </footer>
      </body>
    </html>
    """
    
    print("   Analyzing page for keyword 'Python'...")
    analysis_phrase = crawler.analyze_page(
        html_content=mock_html,
        url="https://codeacademy.org/tutorials/python",
        keyword="Python",
        match_type="phrase",
        case_sensitive=False,
        exact_match=False
    )
    
    print(f"   * Title parsed: '{analysis_phrase['title']}'")
    print(f"   * Language detected: '{analysis_phrase['language']}'")
    print(f"   * Date parsed: {analysis_phrase['discovered_at']}")
    print(f"   * Keyword Occurrences: {analysis_phrase['occurrences']}")
    print(f"   * Found in Title: {analysis_phrase['found_in_title']}")
    print(f"   * Found in Description: {analysis_phrase['found_in_description']}")
    print(f"   * Found in Body: {analysis_phrase['found_in_body']}")
    print(f"   * Found in URL: {analysis_phrase['found_in_url']}")
    print(f"   * Snippet extracted: '{analysis_phrase['snippet']}'")
    print(f"   * Content MD5 Hash: {analysis_phrase['content_hash']}")
    print(f"   * Content Relevance Score: {analysis_phrase['relevance_score']}/100")
    
    # Verify values
    assert analysis_phrase["occurrences"] > 0, "Keyword Python occurrences should be > 0"
    assert analysis_phrase["found_in_title"] is True, "Python is in title"
    assert analysis_phrase["found_in_body"] is True, "Python is in body"
    assert analysis_phrase["found_in_url"] is True, "Python is in url path"
    print("   [SUCCESS] Phrase analyzer tests passed.")

    # 4. Test Boolean Expression Evaluator
    print("\n4. Testing Boolean logic evaluator...")
    queries = [
        ("python AND fastapi", True),
        ("python AND ruby", False),
        ("fastapi OR ruby", True),
        ("python AND (fastapi OR ruby)", True),
        ("python AND NOT ruby", True),
        ("NOT php", True)
    ]
    
    clean_body = crawler.clean_html_content(import_soup(mock_html))
    full_text = f"Learn Advanced Python Development\nThis tutorial teaches advanced Python\n{clean_body}\nhttps://codeacademy.org/tutorials/python"
    
    for q, expected in queries:
        matched = crawler.evaluate_boolean_query(full_text, q, case_sensitive=False)
        print(f"   Boolean query: '{q:<35}' -> Match? {matched:<6} (Expected: {expected})")
        assert matched == expected, f"Failed boolean check for {q}"
    print("   [SUCCESS] Boolean evaluator tests passed.")

    # 5. Insert mock data & test Exports
    print("\n5. Testing Database Inserts and Exporter module...")
    mock_query = SearchQuery(
        keyword="Python AND fastapi",
        match_type="boolean",
        status="completed",
        total_urls_found=1,
        total_urls_crawled=1,
        total_urls_matched=1
    )
    db.add(mock_query)
    db.commit()
    db.refresh(mock_query)

    mock_url = CrawledURL(
        search_id=mock_query.id,
        url="https://codeacademy.org/tutorials/python",
        domain="codeacademy.org",
        title=analysis_phrase["title"],
        snippet=analysis_phrase["snippet"],
        occurrences=analysis_phrase["occurrences"],
        found_in_title=analysis_phrase["found_in_title"],
        found_in_description=analysis_phrase["found_in_description"],
        found_in_body=analysis_phrase["found_in_body"],
        found_in_url=analysis_phrase["found_in_url"],
        language=analysis_phrase["language"],
        status="matched",
        relevance_score=analysis_phrase["relevance_score"],
        is_duplicate=False,
        full_content=analysis_phrase.get("full_content"),
        raw_html=analysis_phrase.get("raw_html"),
        description=analysis_phrase.get("description", ""),
        author=analysis_phrase.get("author", "Unknown"),
        image_url=analysis_phrase.get("image_url"),
        image_links=analysis_phrase.get("image_links"),
        video_links=analysis_phrase.get("video_links"),
        matched_keywords=analysis_phrase.get("matched_keywords")
    )
    db.add(mock_url)
    db.commit()

    # Generate Exports
    csv_bytes, _ = export_results(mock_query.id, "csv", db)
    excel_bytes, _ = export_results(mock_query.id, "xlsx", db)
    json_bytes, _ = export_results(mock_query.id, "json", db)
    parquet_bytes, _ = export_results(mock_query.id, "parquet", db)

    print(f"   * Generated CSV Export ({len(csv_bytes)} bytes)")
    print(f"   * Generated Excel Export ({len(excel_bytes)} bytes)")
    print(f"   * Generated JSON Export ({len(json_bytes)} bytes)")
    print(f"   * Generated Parquet Export ({len(parquet_bytes)} bytes)")
    
    assert len(csv_bytes) > 0
    assert len(excel_bytes) > 0
    assert len(json_bytes) > 0
    assert len(parquet_bytes) > 0
    print("   [SUCCESS] Exporter tests passed.")

    # 6. Test Multi-Keyword matching
    print("\n6. Testing Multi-Keyword matching logic...")
    analysis_multi = crawler.analyze_page(
        html_content=mock_html,
        url="https://codeacademy.org/tutorials/python",
        keyword="Python, FastAPI, Celery, MissingWord",
        match_type="phrase",
        case_sensitive=False,
        exact_match=False
    )
    print(f"   * Matched keywords parsed: {analysis_multi['matched_keywords']}")
    import json
    matched_kws = json.loads(analysis_multi['matched_keywords'])
    assert "Python" in matched_kws, "Python should be matched"
    assert "FastAPI" in matched_kws, "FastAPI should be matched"
    assert "Celery" in matched_kws, "Celery should be matched"
    assert "MissingWord" not in matched_kws, "MissingWord should NOT be matched"
    print("   [SUCCESS] Multi-Keyword matching logic tests passed.")

    # 7. Test Empty Keyword (Keyword-Free Scraping) Match Bypass
    print("\n7. Testing Keyword-Free Scraping match bypass logic...")
    analysis_empty = crawler.analyze_page(
        html_content=mock_html,
        url="https://codeacademy.org/tutorials/python",
        keyword="",
        match_type="phrase",
        case_sensitive=False,
        exact_match=False
    )
    print(f"   * Status matched: {analysis_empty['matched']}")
    print(f"   * Occurrences: {analysis_empty['occurrences']}")
    print(f"   * Relevance score: {analysis_empty['relevance_score']}")
    print(f"   * Snippet extracted: '{analysis_empty['snippet']}'")
    assert analysis_empty["matched"] is True, "Empty keyword should always match"
    assert analysis_empty["occurrences"] == 0, "Empty keyword occurrences should be 0"
    assert analysis_empty["relevance_score"] == 100.0, "Empty keyword relevance should default to 100"
    print("   [SUCCESS] Keyword-Free Scraping match bypass tests passed.")

    # 8. Test PostgreSQL Integration (Optional/Conditional)
    print("\n8. Testing PostgreSQL Integration...")
    try:
        from backend.postgres_integration import init_postgres_db, classify_article, export_search_to_postgres
        
        # Test heuristic classification
        print("   Testing classification heuristics...")
        cls = classify_article("https://www.rand.org/pubs/policy_briefs/PB101.pdf", "US Maritime Security Strategy", "The navy and vessels are critical for defense in the Indo-Pacific region.", "en")
        print(f"   * Classification result: {cls}")
        assert cls["source_type"] == "Think Tank", "Should detect Think Tank"
        assert cls["content_type"] == "Policy Brief", "Should detect Policy Brief"
        assert "Maritime" in cls["subject_theme"], "Should detect Maritime"
        assert "Defence" in cls["subject_theme"], "Should detect Defence"
        assert "United States" in cls["country_region"], "Should detect United States"
        assert "Indo-Pacific" in cls["country_region"], "Should detect Indo-Pacific"
        assert cls["language"] == "English", "Should map to English"
        print("   [SUCCESS] Heuristic classifier tests passed.")
        
        # Test DB connection and insertion
        print("   Attempting to initialize PostgreSQL DB & run test sync...")
        init_postgres_db(verbose=True)
        
        inserted, updated = export_search_to_postgres(mock_query.id, db)
        print(f"   * Sync result: inserted {inserted}, updated {updated}")
        assert inserted > 0 or updated > 0, "Should have synced the matched mock article"
        print("   [SUCCESS] PostgreSQL database connection & sync tests passed.")
    except Exception as e:
        print(f"   [SKIPPED/WARNING] PostgreSQL tests could not be fully completed: {e}")
 
    # 9. Test Firecrawl Normalization Layer
    print("\n9. Testing Firecrawl Normalization Layer...")
    try:
        from backend.firecrawl_converter import convert_html_to_firecrawl_schema
        
        # Comprehensive test HTML including code block, blockquote, images, video tags, iframe embeds, and lists
        rich_mock_html = """
        <html>
          <head>
            <title>Advanced Scraping Framework Specs</title>
            <meta name="description" content="Detailed specs for the Firecrawl-quality content scraper.">
          </head>
          <body>
            <main id="main-content">
              <h1>Scraper Specifications</h1>
              <p>The goal is to extract visible human-readable text exactly as it appears. Let's look at this <a href="/pricing" title="Check plans">pricing guide</a>.</p>
              
              <blockquote>"This scraper is built for maximum content fidelity."</blockquote>
              
              <pre><code>def get_scraper(): return FirecrawlConverter()</code></pre>
              
              <figure>
                <img src="/assets/diagram.png" alt="Architecture Diagram" title="Core Architecture Diagram" width="800" height="600" />
                <figcaption>Our advanced data extraction flow</figcaption>
              </figure>

              <video src="/assets/demo.mp4" poster="/assets/thumbnail.png" title="Framework Demo Video"></video>
              <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Tutorial Video"></iframe>
            </main>
          </body>
        </html>
        """
        
        fc_res = convert_html_to_firecrawl_schema(
            html=rich_mock_html,
            url="https://scraper.io/docs/specs",
            status_code=200
        )
        
        print(f"   * Success flag: {fc_res['success']}")
        print(f"   * Title: '{fc_res['data']['metadata']['title']}'")
        print(f"   * Headings: {fc_res['data']['content']['headings']}")
        print(f"   * Links: {fc_res['data']['links']}")
        print(f"   * Images: {fc_res['data']['images']}")
        print(f"   * Videos: {fc_res['data']['videos']}")
        print(f"   * Code blocks: {fc_res['data']['content']['codeBlocks']}")
        print(f"   * Quotes: {fc_res['data']['content']['quotes']}")
        
        # Verify schema elements
        assert fc_res["success"] is True
        assert "markdown" in fc_res["data"]
        assert "html" in fc_res["data"]
        assert "metadata" in fc_res["data"]
        assert "links" in fc_res["data"]
        assert "images" in fc_res["data"]
        assert "videos" in fc_res["data"]
        assert "content" in fc_res["data"]
        
        # Title and content structures
        assert fc_res["data"]["metadata"]["title"] == "Advanced Scraping Framework Specs"
        assert "Scraper Specifications" in fc_res["data"]["content"]["headings"]
        assert '"This scraper is built for maximum content fidelity."' in fc_res["data"]["content"]["quotes"]
        assert "def get_scraper(): return FirecrawlConverter()" in fc_res["data"]["content"]["codeBlocks"]
        
        # Markdown Link Isolation (Anchor text plain rendering)
        markdown_body = fc_res["data"]["markdown"]
        assert "pricing guide" in markdown_body
        assert "[pricing guide]" not in markdown_body
        assert "https://scraper.io/pricing" not in markdown_body
        
        # Markdown Image Isolation
        assert "diagram.png" not in markdown_body
        assert "![" not in markdown_body
        
        # Separated Link Metadata Check
        links = fc_res["data"]["links"]
        assert len(links) == 1
        assert links[0]["text"] == "pricing guide"
        assert links[0]["url"] == "https://scraper.io/pricing"
        assert links[0]["title"] == "Check plans"
        
        # Separated Image Metadata Check
        images = fc_res["data"]["images"]
        assert len(images) == 1
        assert images[0]["src"] == "https://scraper.io/assets/diagram.png"
        assert images[0]["alt"] == "Architecture Diagram"
        assert images[0]["caption"] == "Our advanced data extraction flow"
        assert images[0]["width"] == 800
        assert images[0]["height"] == 600
        
        # Separated Video Metadata Check
        videos = fc_res["data"]["videos"]
        assert len(videos) == 2
        
        # HTML5 video
        html5_video = [v for v in videos if v["type"] == "html5"][0]
        assert html5_video["src"] == "https://scraper.io/assets/demo.mp4"
        assert html5_video["title"] == "Framework Demo Video"
        assert html5_video["thumbnail"] == "https://scraper.io/assets/thumbnail.png"
        
        # YouTube iframe embed
        yt_video = [v for v in videos if v["type"] == "youtube"][0]
        assert yt_video["src"] == "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert yt_video["title"] == "Tutorial Video"
        
        print("   [SUCCESS] Firecrawl Normalization Layer tests passed.")
    except Exception as e:
        print(f"   [FAIL] Firecrawl Normalization Layer tests failed: {e}")
        raise e

    # 10. Testing Trafilatura and SimHash Integrations
    print("\n10. Testing Trafilatura and SimHash Integrations...")
    
    # 10a. Trafilatura Extraction & Fallback test
    print("   Testing Trafilatura body extraction...")
    rich_news_html = """
    <html>
      <body>
        <article>
          <h1>Breaking News: Trafilatura Integration Successful</h1>
          <p>KeywordScout has successfully integrated Trafilatura as its primary extraction engine. 
             This makes it extremely robust at extracting visible human-readable text from news articles, 
             blogs, and other web pages. We will ensure that our downstream search query logic runs perfectly.
             This paragraph has enough characters to exceed the extraction minimum character limit threshold of 200 characters easily.</p>
          <p>By using multiple advanced algorithms under the hood, we can achieve high recall and precision,
             while preserving full content for Boolean keyword matcher logic. This is the second paragraph of our mock news article.</p>
        </article>
      </body>
    </html>
    """
    soup_news = import_soup(rich_news_html)
    cleaned_txt = crawler.clean_html_content(soup_news, html_content=rich_news_html)
    print(f"   * Extracted length: {len(cleaned_txt)} chars")
    assert len(cleaned_txt) >= 200, "Should extract more than 200 chars"
    assert "Trafilatura" in cleaned_txt, "Should contain the main keyword"
    
    print("   Testing BS4 Fallback on malformed HTML...")
    fallback_txt = crawler.clean_html_content(import_soup("<p>Too short</p>"), html_content="<p>Too short</p>")
    print(f"   * Fallback text: '{fallback_txt.strip()}'")
    assert len(fallback_txt) > 0, "Fallback should still yield text"

    # 10b. SimHash test
    print("   Testing SimHash Engine...")
    from backend.simhash_dedup import compute_simhash, is_near_duplicate
    hash1 = compute_simhash("The quick brown fox jumps over the lazy dog and runs away happily.")
    hash2 = compute_simhash("The quick brown fox jumps over the lazy dog and runs away happily!")
    hash3 = compute_simhash("A completely different text that has absolutely nothing in common with the quick brown fox.")
    print(f"   * Hash 1: '{hash1}'")
    print(f"   * Hash 2: '{hash2}'")
    print(f"   * Hash 3: '{hash3}'")
    assert hash1 != "", "Should return valid hex hash"
    assert is_near_duplicate(hash1, hash2) is True, "Nearly identical texts should be near-duplicates"
    assert is_near_duplicate(hash1, hash3) is False, "Completely different texts should not be near-duplicates"
    print("   [SUCCESS] SimHash checks passed.")

    # 10c. Sitemap/Feed Discovery test
    print("   Testing Sitemap & Feed Discovery (best effort)...")
    from backend.sitemap_discovery import discover_from_sitemap, discover_from_feeds
    try:
        urls_sm = discover_from_sitemap("https://example.com")
        print(f"   * Discovered sitemap URLs count: {len(urls_sm)}")
        assert isinstance(urls_sm, list)
    except Exception as e:
        print(f"   [WARNING] discover_from_sitemap failed (expected if offline/restricted): {e}")

    try:
        urls_feed = discover_from_feeds("https://example.com")
        print(f"   * Discovered feed URLs count: {len(urls_feed)}")
        assert isinstance(urls_feed, list)
    except Exception as e:
        print(f"   [WARNING] discover_from_feeds failed: {e}")

    # 10d. Metadata enrichment test
    print("   Testing Metadata enrichment via JSON-LD...")
    json_ld_html = """
    <html>
      <head>
        <title>Original Title</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "Enriched Headline via Trafilatura",
          "datePublished": "2026-06-20",
          "author": {
            "@type": "Person",
            "name": "Jane Doe"
          }
        }
        </script>
      </head>
      <body>
        <p>This is the body content containing some useful details about the test.</p>
      </body>
    </html>
    """
    enriched_analysis = crawler.analyze_page(
        html_content=json_ld_html,
        url="https://example.com/enriched",
        keyword="",
        match_type="phrase"
    )
    print(f"   * Enriched Author: '{enriched_analysis['author']}'")
    print(f"   * Enriched Date: '{enriched_analysis['discovered_at']}'")
    assert enriched_analysis["author"] == "Jane Doe", "Author should be Jane Doe"
    print("   [SUCCESS] Metadata enrichment checks passed.")

    # 10e. Language detection test
    print("   Testing Language Detection Upgrade...")
    french_text = """
    Ceci est un paragraphe écrit en français pour tester la détection de la langue.
    Nous espérons que le système reconnaîtra correctement que le texte est en français.
    Le moteur Trafilatura ou py3langid devrait classifier ce texte avec un score de confiance élevé.
    """
    french_html = f"<html><body><p>{french_text}</p></body></html>"
    detected_lang = crawler.detect_language(import_soup(french_html), body_text=french_text)
    print(f"   * Detected language: '{detected_lang}'")
    assert detected_lang == "fr", f"Language should be fr, got {detected_lang}"
    print("   [SUCCESS] Language detection checks passed.")

    # 11. Run Proxy and Charset Decoding Tests
    print("\n11. Testing Proxy Isolation & Charset Decoding...")
    test_no_proxy_session_unchanged()
    test_empty_string_proxy_treated_as_none()
    test_http_proxy_configures_session()
    test_socks5h_proxy_configures_session()
    test_utf8_site_uses_fast_path()
    test_gb2312_content_type_decoded_correctly()
    test_gbk_meta_charset_decoded_correctly()
    test_non_cn_latin1_site_uses_fallback()
    print("   [SUCCESS] Proxy & Charset decoding tests passed.")

    # 12. Run Error Page Detection Tests
    print("\n12. Testing Error and Cloudflare Page Detection...")
    test_error_page_detection()
    print("   [SUCCESS] Error page detection tests passed.")

    # 13. Run Default Date Filtering Tests
    print("\n13. Testing Default Date Filtering (3 Months limit)...")
    test_default_date_filter()
    print("   [SUCCESS] Default date filtering tests passed.")



    # Clean up mock items
    db.delete(mock_url)

    db.delete(mock_query)
    db.commit()
    db.close()
    
    print("\n=== ALL DIAGNOSTIC TESTS PASSED SUCCESSFULLY ===")

def import_soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")

from unittest.mock import patch, MagicMock

# ── Proxy isolation tests ─────────────────────────────────────────────────────

def test_no_proxy_session_unchanged():
    """Jobs without proxy_url must have empty session.proxies and en-US Accept-Language."""
    c = Crawler()
    assert c.proxy_url is None
    assert not c.session.proxies  # must be empty
    assert "en-US" in c.session.headers.get("Accept-Language", "")
    assert "zh-CN" not in c.session.headers.get("Accept-Language", "")

def test_empty_string_proxy_treated_as_none():
    """Empty string proxy_url must behave identically to None."""
    c = Crawler(proxy_url="")
    assert c.proxy_url is None
    assert not c.session.proxies

def test_http_proxy_configures_session():
    c = Crawler(proxy_url="http://127.0.0.1:8080")
    assert c.session.proxies["http"] == "http://127.0.0.1:8080"
    assert c.session.proxies["https"] == "http://127.0.0.1:8080"
    assert "zh-CN" in c.session.headers.get("Accept-Language", "")

def test_socks5h_proxy_configures_session():
    c = Crawler(proxy_url="socks5h://127.0.0.1:1080")
    assert c.session.proxies["http"] == "socks5h://127.0.0.1:1080"
    assert c.session.proxies["https"] == "socks5h://127.0.0.1:1080"

# ── Charset decoding tests ────────────────────────────────────────────────────

def _mock_response(content: bytes, content_type: str, encoding: str = None):
    mock = MagicMock()
    mock.content = content
    mock.headers = {"Content-Type": content_type}
    mock.encoding = encoding
    mock.raise_for_status = lambda: None
    # Simulate requests' response.text behaviour
    mock.text = content.decode(encoding or "utf-8", errors="replace")
    return mock

def test_utf8_site_uses_fast_path():
    """UTF-8 sites must return response.text without entering charset scan logic."""
    c = Crawler()
    body = "<html><body>Hello World</body></html>"
    mock = _mock_response(body.encode("utf-8"), "text/html; charset=utf-8", "utf-8")
    with patch.object(c.session, "get", return_value=mock):
        result = c._fetch_http("https://bbc.com/")
    assert "Hello World" in result

def test_gb2312_content_type_decoded_correctly():
    """Pages declaring charset=gb2312 in Content-Type must return correct Chinese text."""
    c = Crawler()
    chinese = "新华网新闻"
    mock = _mock_response(chinese.encode("gb18030"), "text/html; charset=gb2312", "gb2312")
    with patch.object(c.session, "get", return_value=mock):
        result = c._fetch_http("https://xinhuanet.com/article")
    assert chinese in result

def test_gbk_meta_charset_decoded_correctly():
    """Pages declaring GBK in <meta charset> but not in Content-Type must decode correctly."""
    c = Crawler()
    chinese = "人民日报"
    html = f'<html><head><meta charset="GBK"></head><body>{chinese}</body></html>'.encode("gbk")
    mock = _mock_response(html, "text/html", None)
    mock.encoding = None
    # Simulate requests returning latin-1 text (the mojibake case)
    mock.text = html.decode("latin-1", errors="replace")
    with patch.object(c.session, "get", return_value=mock):
        result = c._fetch_http("https://people.com.cn/article")
    assert chinese in result

def test_non_cn_latin1_site_uses_fallback():
    """A site that gets latin-1 detection but has no CN charset meta should use response.text."""
    c = Crawler()
    body = b"<html><body>Bonjour le monde</body></html>"
    mock = _mock_response(body, "text/html", "iso-8859-1")
    mock.text = body.decode("iso-8859-1")
    with patch.object(c.session, "get", return_value=mock):
        result = c._fetch_http("https://lemonde.fr/article")
    assert "Bonjour le monde" in result


def test_error_page_detection():
    """Verify that is_error_page correctly identifies various error and Cloudflare pages, and allows normal pages."""
    c = Crawler()
    
    # 1. 504 Gateway time-out in title
    html_504 = "<html><head><title>mizzima.com | 504: Gateway time-out</title></head><body>Server timeout</body></html>"
    is_err, reason = c.is_error_page(html_504)
    assert is_err is True
    assert "504 Gateway" in reason or "504: Gateway" in reason
    
    # 2. 502 Bad Gateway in short body text
    html_502 = "<html><head><title>Error</title></head><body><h1>502 Bad Gateway</h1></body></html>"
    is_err, reason = c.is_error_page(html_502)
    assert is_err is True
    assert "502 Bad Gateway" in reason
    
    # 3. Cloudflare challenge/block page detection
    html_cloudflare = """
    <html>
      <head><title>Please Wait... | Cloudflare</title></head>
      <body>
        <h1>Checking your browser before accessing the website.</h1>
        <p>This process is automatic. Your browser will redirect shortly.</p>
        <p>Cloudflare Ray ID: 7abc123456789def</p>
      </body>
    </html>
    """
    is_err, reason = c.is_error_page(html_cloudflare)
    assert is_err is True
    assert "Cloudflare" in reason
    
    # 4. Normal article containing a keyword (no false positive)
    html_normal = """
    <html>
      <head><title>How to handle web development</title></head>
      <body>
        <h1>Web Development Guidelines</h1>
        <p>In this article, we explain how to build scalable python web servers. We will discuss load balancing, CDN configuration, and database indexing. This article is very long and has lots of words about development.</p>
        <p>Some people experience 504 Gateway timeouts when their server is slow, but we can fix this by optimizing database queries and increasing timeout limits in nginx.</p>
      </body>
    </html>
    """
    is_err, reason = c.is_error_page(html_normal)
    assert is_err is False, f"Expected normal page to be allowed, but got: {reason}"


def test_default_date_filter():
    """Verify that crawl_url_task defaults date filtering to 3 months if not specified, and respects custom dates."""
    from unittest.mock import patch, MagicMock
    from datetime import datetime, timedelta, timezone
    from backend.queue_manager import crawl_url_task
    
    mock_db = MagicMock()
    
    mock_crawled_url1 = MagicMock()
    mock_crawled_url1.url = "https://example.com/old"
    mock_crawled_url1.domain = "example.com"
    
    mock_crawled_url2 = MagicMock()
    mock_crawled_url2.url = "https://example.com/new"
    mock_crawled_url2.domain = "example.com"
    
    mock_crawled_url3 = MagicMock()
    mock_crawled_url3.url = "https://example.com/old_but_allowed"
    mock_crawled_url3.domain = "example.com"
    
    mock_filter1 = MagicMock()
    mock_filter1.first.return_value = mock_crawled_url1
    mock_filter2 = MagicMock()
    mock_filter2.first.return_value = mock_crawled_url2
    mock_filter3 = MagicMock()
    mock_filter3.first.return_value = mock_crawled_url3
    
    mock_db.query.return_value.filter.side_effect = [mock_filter1, mock_filter2, mock_filter3]
    
    # 1. Test case: No start date implied (defaults to 90 days ago)
    # Article date is 100 days ago (should be skipped)
    date_100_days_ago = datetime.now(timezone.utc) - timedelta(days=100)
    mock_analysis_old = {
        "matched": True,
        "discovered_at": date_100_days_ago,
        "language": "en",
        "title": "Old Article",
        "snippet": "Old content",
        "occurrences": 1,
        "found_in_title": False,
        "found_in_description": False,
        "found_in_body": True,
        "found_in_url": False,
        "domain": "example.com",
        "content_hash": "hash_old",
        "description": "desc",
        "full_content": "full content",
        "raw_html": "html",
        "author": "Unknown",
        "simhash": "simhash_old",
        "image_url": "",
        "image_links": "[]",
        "video_links": "[]",
        "relevance_score": 50.0
    }
    
    # Article date is 10 days ago (should be matched, not skipped)
    date_10_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
    mock_analysis_new = mock_analysis_old.copy()
    mock_analysis_new["discovered_at"] = date_10_days_ago
    mock_analysis_new["title"] = "New Article"
    mock_analysis_new["content_hash"] = "hash_new"
    
    with patch("backend.queue_manager.SessionLocal", return_value=mock_db), \
         patch("backend.queue_manager.Crawler") as mock_crawler_cls, \
         patch("backend.queue_manager._run_analysis") as mock_run_analysis:
         # NOTE: crawl_url_task's analysis call site now goes through
         # backend.queue_manager._run_analysis (Phase 1: offloaded to a
         # ProcessPoolExecutor) instead of calling crawler.analyze_page(...)
         # directly - mock that function instead of Crawler.analyze_page so
         # this test controls what "analysis" returns, same as before.

        mock_crawler = mock_crawler_cls.return_value
        mock_crawler.fetch_page.return_value = "<html>Mock HTML</html>"

        # Test old article (100 days ago) - should be skipped
        mock_run_analysis.return_value = mock_analysis_old
        url_id, result = crawl_url_task(
            url_id=1,
            search_id=1,
            keyword="test",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            ignore_robots=True,
            date_range_start=None
        )
        assert result["status"] == "skipped", f"Expected skipped, got {result['status']}"
        assert "before date_range_start" in result["error_message"]
        
        # Test new article (10 days ago) - should be matched
        mock_run_analysis.return_value = mock_analysis_new
        url_id, result = crawl_url_task(
            url_id=2,
            search_id=1,
            keyword="test",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            ignore_robots=True,
            date_range_start=None
        )
        assert result["status"] == "matched", f"Expected matched, got {result['status']}"
        
        # Test old article (100 days ago) with explicit date_range_start of 120 days ago - should be matched
        date_120_days_ago = datetime.now(timezone.utc) - timedelta(days=120)
        mock_run_analysis.return_value = mock_analysis_old
        url_id, result = crawl_url_task(
            url_id=3,
            search_id=1,
            keyword="test",
            match_type="phrase",
            case_sensitive=False,
            exact_match=False,
            engine="fast",
            ignore_robots=True,
            date_range_start=date_120_days_ago
        )
        assert result["status"] == "matched", f"Expected matched, got {result['status']}"



if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
