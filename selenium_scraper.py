import os
import re
import time
import json
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from selenium import webdriver
from backend.postgres_integration import classify_article
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException
)

# Regex patterns that indicate related articles, comments, or other sections that come after the main article
STOP_PATTERNS = [
    re.compile(r'^more\s+(?:.*\s+)?(?:news|articles|stories|headlines)$', re.IGNORECASE),
    re.compile(r'^related\s+(?:.*\s+)?(?:stories|articles|posts|news|content)$', re.IGNORECASE),
    re.compile(r'^you\s+may\s+also\s+like$', re.IGNORECASE),
    re.compile(r'^recommended\s+for\s+you$', re.IGNORECASE),
    re.compile(r'^read\s+next$', re.IGNORECASE),
    re.compile(r'^sponsored\s+(?:.*\s+)?content$', re.IGNORECASE),
    re.compile(r'^latest\s+(?:.*\s+)?(?:stories|news|headlines|articles)$', re.IGNORECASE),
    re.compile(r'^popular\s+(?:.*\s+)?(?:stories|articles|posts)$', re.IGNORECASE),
    re.compile(r'^top\s+stories$', re.IGNORECASE),
    re.compile(r'^trending\s+(?:.*\s+)?(?:stories|news|topics)$', re.IGNORECASE),
    re.compile(r'^comments$', re.IGNORECASE),
    re.compile(r'^discussion$', re.IGNORECASE),
    re.compile(r'^share\s+this\s+article$', re.IGNORECASE),
    re.compile(r'^follow\s+us$', re.IGNORECASE),
    re.compile(r'^newsletter$', re.IGNORECASE),
    re.compile(r'^more\s+from\s+', re.IGNORECASE),
]

# Common selectors and text patterns for expand/read-more buttons
EXPAND_BUTTON_XPATHS = [
    "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'read more')]",
    "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'show more')]",
    "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'expand')]",
    "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'view full article')]",
    "//button[contains(@class, 'read-more') or contains(@class, 'expand') or contains(@class, 'show-more')]",
    "//a[contains(@class, 'read-more') or contains(@class, 'expand') or contains(@class, 'show-more')]"
]

# Common pagination xpath selectors for "Next" buttons or anchors
NEXT_PAGE_XPATHS = [
    "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
    "//a[contains(@class, 'next') or contains(@class, 'pagination-next') or contains(@id, 'next')]",
    "//button[contains(@class, 'next') or contains(@class, 'pagination-next') or contains(@id, 'next')]",
    "//a[text()='>' or text()='»' or contains(text(), 'Next') or contains(text(), 'next')]",
    "//link[@rel='next']"
]

def setup_driver(headless: bool = True) -> webdriver.Chrome:
    """Initializes and configures the Selenium Chrome WebDriver."""
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new") # Modern headless mode
    
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Exclude the collection of enable-automation switches
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(options=options)
    
    # Enable stealth mode (bypass basic bot detection)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    
    return driver

def is_error_page(page_source: str, title: str) -> tuple[bool, str]:
    """
    Detects if the given page source or title represents an error page.
    """
    if not page_source or not page_source.strip():
        return True, "Empty page source"
        
    title_lower = title.lower() if title else ""
    
    error_indicators = [
        ("504 gateway time-out", "504 Gateway Time-out"),
        ("504 gateway timeout", "504 Gateway Timeout"),
        ("502 bad gateway", "502 Bad Gateway"),
        ("503 service unavailable", "503 Service Unavailable"),
        ("503 service temporarily unavailable", "503 Service Temporarily Unavailable"),
        ("500 internal server error", "500 Internal Server Error"),
        ("403 forbidden", "403 Forbidden"),
        ("404 not found", "404 Not Found"),
        ("database error", "Database Error"),
        ("database connection error", "Database Connection Error"),
        ("error establishing a database connection", "Database Connection Error"),
        ("site is down", "Site is down"),
        ("maintenance mode", "Maintenance Mode"),
        ("access denied", "Access Denied"),
        ("gateway time-out", "Gateway Time-out"),
        ("gateway timeout", "Gateway Timeout"),
        ("bad gateway", "Bad Gateway"),
    ]
    
    for pattern_lower, label in error_indicators:
        if pattern_lower in title_lower:
            if len(title_lower) < 120:
                return True, f"{label} detected in page title: '{title}'"
                
    # Clean text
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page_source, "html.parser")
    for tag in soup(["script", "style", "head", "iframe"]):
        tag.decompose()
    body_text = soup.get_text()
    body_text = " ".join(body_text.split())
    body_text_lower = body_text.lower()
    
    if len(body_text) < 1200:
        for pattern_lower, label in error_indicators:
            idx = body_text_lower.find(pattern_lower)
            if idx != -1 and idx < 120:
                return True, f"{label} detected in short body text"
            elif len(body_text) < 250 and pattern_lower in body_text_lower:
                return True, f"{label} detected in extremely short body text"
                
    if "cloudflare" in body_text_lower or "cloudflare" in title_lower:
        cf_signals = [
            "ray id", "security check", "ddos protection", "verify you are human",
            "checking your browser", "enable cookies", "enable javascript",
            "captcha", "turnstile", "unusual traffic", "browser validation"
        ]
        matched_signals = [sig for sig in cf_signals if sig in body_text_lower]
        if len(matched_signals) >= 2 or (len(body_text) < 1500 and len(matched_signals) >= 1):
            return True, f"Cloudflare block/challenge page detected (signals: {matched_signals})"
            
    return False, ""


def check_keywords_in_page(page_source: str, keywords: list) -> bool:
    """Checks if any of the target keywords exist anywhere on the page."""
    source_lower = page_source.lower()
    for kw in keywords:
        if kw.lower() in source_lower:
            return True
    return False

def expand_and_scroll_to_bottom(driver: webdriver.Chrome):
    """
    Scrolls down the page progressively to trigger lazy-loaded elements,
    and searches for / clicks any 'Read More' or 'Expand' buttons.
    """
    print("[INFO] Initiating dynamic scrolling and article expansion...")
    last_height = driver.execute_script("return document.body.scrollHeight")
    scroll_pause_time = 1.5
    no_change_count = 0
    max_no_change = 4  # Maximum attempts to scroll when height doesn't change

    while True:
        # Scroll down to the bottom
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(scroll_pause_time)

        # Look for any "Read More" or "Expand" buttons
        for xpath in EXPAND_BUTTON_XPATHS:
            try:
                # Use a short explicit wait to check if the button is present and visible
                wait = WebDriverWait(driver, 1.5)
                button = wait.until(EC.visibility_of_element_located((By.XPATH, xpath)))
                
                # Check if it's displayed and enabled before clicking
                if button.is_displayed() and button.is_enabled():
                    print(f"[INFO] Found expand button: '{button.text.strip() or 'XPath match'}'. Attempting to click...")
                    try:
                        button.click()
                        time.sleep(1.0)
                    except (ElementClickInterceptedException, StaleElementReferenceException):
                        # Fallback to JavaScript click if intercepted or stale
                        print("[WARNING] Click intercepted. Falling back to JavaScript click...")
                        driver.execute_script("arguments[0].click();", button)
                        time.sleep(1.0)
            except TimeoutException:
                continue
            except Exception as e:
                print(f"[DEBUG] Error checking button: {e}")

        # Calculate new scroll height and compare with last scroll height
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            no_change_count += 1
            if no_change_count >= max_no_change:
                print("[INFO] Reached the true bottom of the expanded page.")
                break
        else:
            no_change_count = 0
            last_height = new_height

def is_valid_video_url(url: str) -> bool:
    """Helper to detect if a URL points to a standard video hosting platform or asset."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if any(p in domain for p in ["youtube.com", "youtu.be", "vimeo.com", "dailymotion.com"]):
        return True
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in [".mp4", ".webm", ".ogg", ".mov"]):
        return True
    return False

def find_and_click_next_page(driver: webdriver.Chrome, timeout: int = 5) -> bool:
    """
    Searches for common pagination 'Next' elements and clicks them.
    Returns True if next page navigation succeeded, False otherwise.
    """
    print("[INFO] Searching for 'Next' page pagination link...")
    for xpath in NEXT_PAGE_XPATHS:
        try:
            wait = WebDriverWait(driver, timeout)
            # Find elements matching xpath
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    # Check if it is a tag with disabled attribute or class (e.g., class="disabled")
                    parent = el
                    is_disabled = False
                    # Check up to 3 parent levels for 'disabled' indicators
                    for _ in range(3):
                        if parent is None:
                            break
                        cl = parent.get_attribute("class") or ""
                        disabled_attr = parent.get_attribute("disabled")
                        if "disabled" in cl.lower() or disabled_attr is not None:
                            is_disabled = True
                            break
                        try:
                            parent = parent.find_element(By.XPATH, "..")
                        except Exception:
                            parent = None
                    
                    if is_disabled:
                        continue
                        
                    # Target found and active! Get current URL to compare after navigation
                    current_url = driver.current_url
                    print(f"[INFO] Found Next page element matching XPath: '{xpath}'. Clicking...")
                    
                    try:
                        # Try standard click
                        el.click()
                    except (ElementClickInterceptedException, StaleElementReferenceException):
                        # JS Click fallback
                        driver.execute_script("arguments[0].click();", el)
                    
                    # Wait for URL to change or page to load
                    time.sleep(2.0)
                    
                    # Verify if navigation actually changed the URL or modified page source
                    if driver.current_url != current_url:
                        print(f"[INFO] Navigation successful. New URL: {driver.current_url}")
                        return True
                    else:
                        print("[WARNING] Click did not change the URL. Trying next candidate...")
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException):
            continue
        except Exception as e:
            print(f"[DEBUG] Error checking next button: {e}")
            
    return False

def extract_article_data(driver: webdriver.Chrome, url: str, keywords: list) -> dict:
    """Extracts data conforming to the 19-field schema from the fully expanded page."""
    print("[INFO] Extracting data from page...")
    
    # 1. Text Content Extraction
    text_elements = driver.find_elements(By.XPATH, "//h1 | //h2 | //h3 | //h4 | //h5 | //h6 | //p")
    text_lines = []
    for el in text_elements:
        try:
            txt = el.text.strip()
            if txt:
                tag_name = el.tag_name.lower()
                is_heading = tag_name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']
                if is_heading or len(txt) < 80:
                    is_stop = False
                    for pattern in STOP_PATTERNS:
                        if pattern.match(txt):
                            is_stop = True
                            break
                    if is_stop:
                        print(f"[INFO] Section header '{txt}' matched stop pattern. Stopping text extraction.")
                        break
                text_lines.append(txt)
        except StaleElementReferenceException:
            continue
            
    full_text = "\n\n".join(text_lines)

    # 2. Video URLs Extraction
    video_urls = set()
    
    # Look in <video> elements
    for el in driver.find_elements(By.TAG_NAME, "video"):
        try:
            src = el.get_attribute("src")
            if src:
                video_urls.add(urljoin(url, src))
            # Check source tags inside video element
            for source in el.find_elements(By.TAG_NAME, "source"):
                source_src = source.get_attribute("src")
                if source_src:
                    video_urls.add(urljoin(url, source_src))
        except Exception:
            continue

    # Look in <iframe> elements for embedded video links (YouTube, Vimeo, etc.)
    for el in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            src = el.get_attribute("src")
            if src and is_valid_video_url(src):
                video_urls.add(src)
        except Exception:
            continue

    # Look for attributes in divs/sections that might have video data sources
    for tag in ["div", "section", "a"]:
        for el in driver.find_elements(By.TAG_NAME, tag):
            try:
                for attr in ["data-src", "data-video", "data-href", "href"]:
                    val = el.get_attribute(attr)
                    if val and is_valid_video_url(val):
                        video_urls.add(urljoin(url, val))
            except Exception:
                continue

    # 3. Image URLs Extraction
    image_urls = set()
    for el in driver.find_elements(By.TAG_NAME, "img"):
        try:
            # Check src and other lazy-loading attributes
            for attr in ["src", "data-src", "data-original-src", "srcset"]:
                src = el.get_attribute(attr)
                if src:
                    # Clean srcset to take first image URL
                    clean_src = src.split(",")[0].split(" ")[0].strip()
                    if clean_src.startswith("http") or clean_src.startswith("/"):
                        image_urls.add(urljoin(url, clean_src))
                        break
        except Exception:
            continue

    # 4. Conform to the 19-field schema
    record_id = hashlib.md5(url.encode("utf-8")).hexdigest()
    
    # Extract Title
    title = ""
    try:
        title = driver.title
    except Exception:
        pass
    if not title:
        try:
            h1s = driver.find_elements(By.TAG_NAME, "h1")
            if h1s:
                title = h1s[0].text.strip()
        except Exception:
            pass
    if not title:
        title = "Untitled"
        
    # Extract Author
    author = "Unknown"
    try:
        author_meta = driver.find_elements(By.XPATH, "//meta[@name='author'] | //meta[@property='article:author']")
        for el in author_meta:
            val = el.get_attribute("content")
            if val:
                author = val.strip()
                break
    except Exception:
        pass
        
    # Extract Language
    language_code = "en"
    try:
        html_tags = driver.find_elements(By.TAG_NAME, "html")
        if html_tags:
            val = html_tags[0].get_attribute("lang")
            if val:
                language_code = val.split("-")[0].strip().lower()
    except Exception:
        pass
        
    source_name = urlparse(url).netloc.lower()
    if source_name.startswith("www."):
        source_name = source_name[4:]
        
    # Classify page heuristically
    classification = classify_article(url, title, full_text, language_code)
    
    keywords_str = ", ".join(keywords) if keywords else ""
    
    return {
        "record_id": record_id,
        "source_name": source_name,
        "source_type": classification["source_type"],
        "title": title,
        "url": url,
        "publication_date": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "content_type": classification["content_type"],
        "subject_theme": classification["subject_theme"],
        "country_region": classification["country_region"],
        "language": classification["language"],
        "keywords": keywords_str,
        "full_text": full_text,
        "tags": keywords_str,
        "pdf_link": url if url.lower().endswith(".pdf") else "",
        "image_links": ", ".join(list(image_urls)),
        "video_links": ", ".join(list(video_urls)),
        "organization": source_name,
        "scraped_date": datetime.now(timezone.utc).isoformat()
    }

def scrape_articles(urls: list, keywords: list, headless: bool = True, max_pages_to_search: int = 10) -> list:
    """
    Iterates through a list of starting URLs. For each URL:
    - Checks for keywords on the current page.
    - If found, scrolls/expands and extracts data, then moves to the next starting URL.
    - If not found, navigates to the 'Next' page using pagination, up to max_pages_to_search times.
    """
    driver = setup_driver(headless=headless)
    scraped_results = []
    
    try:
        for idx, start_url in enumerate(urls, 1):
            print(f"\n==================================================")
            print(f"[{idx}/{len(urls)}] Starting pagination search for: {start_url}")
            print(f"==================================================")
            
            # Navigate to the initial URL
            try:
                driver.get(start_url)
                time.sleep(2.0)
            except Exception as e:
                print(f"[ERROR] Failed to load starting URL {start_url}: {e}")
                continue
                
            page_num = 1
            keyword_found = False
            
            while page_num <= max_pages_to_search:
                print(f"[INFO] Checking Page {page_num} of current URL stream (URL: {driver.current_url})...")
                
                try:
                    # Validate Keywords existence in raw page source
                    source = driver.page_source
                    is_err, reason = is_error_page(source, driver.title)
                    if is_err:
                        print(f"[ERROR] Error/Timeout page detected: {reason}")
                        break
                        
                    if check_keywords_in_page(source, keywords):
                        print(f"[MATCH] Target keywords found on Page {page_num}!")
                        keyword_found = True
                        break
                    
                    print(f"[NO MATCH] Keywords not found on Page {page_num}.")
                    
                    # Look for Next page pagination
                    if page_num < max_pages_to_search:
                        has_next = find_and_click_next_page(driver)
                        if not has_next:
                            print(f"[END OF WEBSITE] No active 'Next' page button or link found on Page {page_num}.")
                            break
                        page_num += 1
                    else:
                        print(f"[LIMIT REACHED] Hit max pages limit ({max_pages_to_search}) without finding keywords.")
                        break
                        
                except Exception as e:
                    print(f"[ERROR] Exception during search on Page {page_num}: {e}")
                    break
            
            if keyword_found:
                try:
                    # Step 3: Scroll and expand the article page where keyword matched
                    expand_and_scroll_to_bottom(driver)
                    
                    # Step 4: Extract article contents
                    article_data = extract_article_data(driver, driver.current_url, keywords)
                    article_data["matched_page_index"] = page_num
                    scraped_results.append(article_data)
                    
                    print(f"[SUCCESS] Successfully scraped: {driver.current_url}")
                    print(f"   * Matched Page Number: {page_num}")
                    print(f"   * Paragraphs/headers: {len(article_data['full_text'].splitlines())}")
                    print(f"   * Video URLs: {len(article_data['video_links'].split(', ')) if article_data['video_links'] else 0}")
                    print(f"   * Image URLs: {len(article_data['image_links'].split(', ')) if article_data['image_links'] else 0}")
                except Exception as e:
                    print(f"[ERROR] Failed to extract data from matched page: {e}")
            else:
                print(f"[SKIP] Finished searching URL stream. No keyword matches found in {page_num} pages checked.")
                
    finally:
        print("\n[INFO] Closing WebDriver...")
        driver.quit()
        
    return scraped_results

if __name__ == "__main__":
    # Example usage:
    # Target starting URLs to scrape
    test_urls = [
        "https://en.wikipedia.org/wiki/Web_scraping",
        "https://realpython.com/tutorials/"
    ]
    
    # Target Keywords to validate against
    target_keywords = ["Python", "scraping", "automation"]
    
    # Run Scraper (Standard mode: headless=False / Headless mode: headless=True)
    results = scrape_articles(test_urls, target_keywords, headless=True, max_pages_to_search=5)
    
    # Pretty print output in structured JSON format
    print("\n\n=================== SCRAPED DATA RESULTS ===================")
    print(json.dumps(results, indent=2, ensure_ascii=False))
