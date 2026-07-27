# ## Changes (Chinese Site Proxy Support - Options 2 + 4)
# - Added proxy_url parameter to Crawler.__init__(). Default None = zero behaviour change.
# - Session proxy configured only inside if self.proxy_url: block.
# - Accept-Language overridden to zh-CN only when proxy_url is set (not globally).
# - Added proxy flag to _get_selenium_driver() Chrome options (gated on proxy_url).
# - Added proxy context to _fetch_lightpanda() via browser.new_context(proxy=...).
# - Fixed _fetch_http() Chinese charset decoding: GB18030/GBK/GB2312 → correct UTF-8.
#   Non-Chinese sites hit the fast path (return response.text) with no code change.
# ## Changes (Trafilatura Integration — KeywordScout v2.0 Upgrade)
# - Integrated trafilatura.extract() as primary body text extractor with BS4 fallback.
# - Added trafilatura metadata enrichment for author, title, date fields.
# - compute_simhash() integrated into analyze_page() return dict.
# - Upgraded detect_language to use py3langid before falling back to langdetect.
# ## Changes
# - Implemented thread-safe LRU+TTL cache (24h TTL, 1000 entry max) for robots.txt files.
# - Replaced eval() in evaluate_boolean_query with a recursive descent parser.
# - Configured standard browser headers for requests.Session.
# - Implemented automatic requests-to-Selenium fallback on HTTP errors (e.g., 403 blocks).
# - Thread-protected Selenium driver initialization in _get_selenium_driver.
# - Replaced datetime.utcnow() with datetime.now(timezone.utc).
# - Added langdetect library fallback to detect_language if HTML tags yield None.

import re
import json
import hashlib
import urllib.parse
import urllib.robotparser
import collections
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional, List, Set
import requests
from bs4 import BeautifulSoup

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

# Try importing Selenium modules; handles cases where it is not installed
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# Try importing Playwright for Lightpanda integration (CDP path)
try:
    from playwright.sync_api import sync_playwright
    LIGHTPANDA_AVAILABLE = True
except ImportError:
    LIGHTPANDA_AVAILABLE = False

# Pre-import optional heavy libraries at module load time to avoid per-call import overhead
try:
    import trafilatura as _trafilatura
    from trafilatura.metadata import extract_metadata as _traf_extract_metadata
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    _trafilatura = None
    _traf_extract_metadata = None
    _TRAFILATURA_AVAILABLE = False

try:
    import py3langid as _py3langid
    _PY3LANGID_AVAILABLE = True
except ImportError:
    _py3langid = None
    _PY3LANGID_AVAILABLE = False

# Thread-safe LRU + TTL Cache for robots.txt parsers
# Format: domain: (RobotFileParser, fetched_at_float)
ROBOTS_CACHE: collections.OrderedDict = collections.OrderedDict()
ROBOTS_CACHE_LOCK = threading.Lock()

def get_chrome_user_agent_details() -> Tuple[str, str]:
    """
    Constructs a realistic Chrome User-Agent.
    On Windows: reads actual installed Chrome version from registry.
    On Linux/Mac: uses a current hardcoded version as fallback.
    """
    import sys
    version = "124.0.6367.201"  # Stable fallback for non-Windows / registry miss
    if sys.platform == "win32":
        try:
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon")
                v, _ = winreg.QueryValueEx(key, "version")
                if v:
                    version = v
            except Exception:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome")
                    v, _ = winreg.QueryValueEx(key, "DisplayVersion")
                    if v:
                        version = v
                except Exception:
                    pass
        except ImportError:
            pass  # winreg not available even on win32 — keep fallback
    major_version = version.split(".")[0]
    user_agent = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )
    return user_agent, major_version


def patch_chromedriver_if_needed(driver_path: str):
    """Checks if the chromedriver binary is patched; if not, replaces the cdc_ automation variables."""
    import os
    if not driver_path or not os.path.exists(driver_path):
        return
    try:
        with open(driver_path, 'rb') as f:
            data = f.read()
        
        import re
        pattern = re.compile(b"cdc_[a-zA-Z0-9_]+")
        matches = pattern.findall(data)
        
        if not matches:
            return  # Already patched or different structure
            
        # Replace cdc_ with dog_ to avoid signature detection
        new_data = data
        for match in set(matches):
            replacement = match.replace(b"cdc_", b"dog_")
            new_data = new_data.replace(match, replacement)
            
        with open(driver_path, 'wb') as f:
            f.write(new_data)
        print(f"[Stealth] Successfully patched chromedriver binary: {driver_path}")
    except Exception as e:
        print(f"[Stealth Warning] Failed to patch chromedriver: {e}")

class Crawler:
    def __init__(self, user_agent: str = None, proxy_url: str = None):
        dynamic_ua, major_version = get_chrome_user_agent_details()
        self.user_agent = user_agent or dynamic_ua
        self.proxy_url = proxy_url if proxy_url and proxy_url.strip() else None

        self.session = requests.Session()

        # Transport-level retry for transient network errors only
        # (status_forcelist excludes 403/404 — those need application-level handling)
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        _retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        _adapter = HTTPAdapter(max_retries=_retry_strategy)
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)

        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",        # unchanged default — DO NOT alter for non-proxy
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": f'"Not A(Brand";v="99", "Google Chrome";v="{major_version}", "Chromium";v="{major_version}"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Cache-Control": "max-age=0"
        })

        # ── Proxy configuration (only when proxy_url is supplied) ────────────────
        # When proxy_url is None, self.session.proxies stays empty — identical to
        # current behaviour for all non-proxy jobs. No default, no env variable read.
        if self.proxy_url:
            self.session.proxies.update({
                "http":  self.proxy_url,
                "https": self.proxy_url,
            })
            # Chinese sites serve Chinese content when Accept-Language prefers zh-CN.
            # Only override this header when a proxy is actively configured.
            self.session.headers.update({
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
            })
            print(f"[Proxy] Crawler session configured with proxy: {self.proxy_url}")

        self._driver = None
        self._driver_lock = threading.Lock()

    def _get_selenium_driver(self):
        """Initializes and returns a headless Chrome Selenium driver (thread-safe)."""
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not installed in the current environment.")
        
        with self._driver_lock:
            if self._driver is None:
                chrome_options = Options()
                chrome_options.add_argument("--headless=new")
                chrome_options.add_argument("--window-size=1920,1080")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument(f"--user-agent={self.user_agent}")
                
                # ── Proxy configuration (only when configured) ────────────────
                if self.proxy_url:
                    # Chrome --proxy-server does not accept socks5h:// — strip the 'h'
                    # so socks5h://host:port becomes socks5://host:port for Chrome.
                    chrome_proxy_arg = self.proxy_url.replace("socks5h://", "socks5://")
                    chrome_options.add_argument(f"--proxy-server={chrome_proxy_arg}")

                    # DNS leak prevention for SOCKS5 proxies:
                    # Without this rule, Chrome resolves hostnames via the local OS
                    # DNS resolver before the proxy sees the request — leaking the
                    # queried hostname outside of Tor. This forces all resolution
                    # through the SOCKS5 proxy instead.
                    if "socks5" in self.proxy_url.lower():
                        chrome_options.add_argument(
                            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost"
                        )
                        print("[Proxy] Selenium Chrome: SOCKS5 DNS leak prevention enabled.")

                    print(f"[Proxy] Selenium Chrome configured with proxy: {chrome_proxy_arg}")
                # ─────────────────────────────────────────────────────────────
                
                # Avoid bot detection by hiding automation controls
                chrome_options.add_argument("--disable-blink-features=AutomationControlled")
                chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
                chrome_options.add_experimental_option('useAutomationExtension', False)
                
                # Use webdriver-manager to get driver path automatically
                driver_path = ChromeDriverManager().install()
                # Apply local binary patching to replace cdc_ automation signatures on the fly
                patch_chromedriver_if_needed(driver_path)
                
                service = Service(driver_path)
                self._driver = webdriver.Chrome(service=service, options=chrome_options)
                self._driver.set_page_load_timeout(30)
                self._driver.set_script_timeout(30)
                
                # Execute CDP command to remove the navigator.webdriver property completely
                self._driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                })
            
        return self._driver

    def _fetch_lightpanda(self, url: str) -> str:
        """
        Fetches the HTML of a page using Lightpanda's browser engine via CDP + Playwright.
        Routes through proxy if configured on this Crawler instance.
        """
        if not LIGHTPANDA_AVAILABLE:
            raise RuntimeError("Playwright is not installed in the current environment.")

        endpoint = "ws://localhost:9222"   # unchanged — Lightpanda always runs locally
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(endpoint)
            try:
                # ── Proxy context (only when configured) ─────────────────────────
                if self.proxy_url:
                    context = browser.new_context(proxy={"server": self.proxy_url})
                    print(f"[Proxy] Lightpanda context configured with proxy: {self.proxy_url}")
                else:
                    context = browser.new_context()   # identical to current new_page() behaviour
                # ─────────────────────────────────────────────────────────────────
                page = context.new_page()
                page.goto(url, timeout=30000)
                content = page.content()
                context.close()
                return content
            finally:
                browser.close()

    def _get_robots_parser(self, domain: str, robots_url: str) -> urllib.robotparser.RobotFileParser:
        """Retrieves or fetches the RobotFileParser for a domain using an LRU+TTL cache (24h TTL, 1000 entry max)."""
        import time
        now = time.time()
        
        with ROBOTS_CACHE_LOCK:
            if domain in ROBOTS_CACHE:
                rp, fetched_at = ROBOTS_CACHE[domain]
                # If cache is valid (< 24h), move to end (MRU) and return
                if now - fetched_at < 86400:
                    ROBOTS_CACHE.move_to_end(domain)
                    return rp
                else:
                    # Expired, evict
                    del ROBOTS_CACHE[domain]
            
            # Create and parse new parser
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(robots_url)
            try:
                r = self.session.get(robots_url, timeout=5)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True
            except Exception:
                rp.allow_all = True
                
            # Evict oldest if cache size >= 1000
            while len(ROBOTS_CACHE) >= 1000:
                ROBOTS_CACHE.popitem(last=False)
                
            ROBOTS_CACHE[domain] = (rp, now)
            return rp

    def close(self):
        """Safely shuts down selenium driver if open."""
        lock = getattr(self, "_driver_lock", None)
        if lock:
            with lock:
                driver = getattr(self, "_driver", None)
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    self._driver = None
        else:
            driver = getattr(self, "_driver", None)
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                self._driver = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def is_error_page(self, html: str, soup: BeautifulSoup = None) -> Tuple[bool, str]:
        """
        Detects if the given HTML content represents a server error, DNS error,
        Cloudflare block/challenge page, or other common scraper failure pages.
        Returns (is_error, reason_message).
        Accepts a pre-parsed soup to avoid redundant parsing.
        """
        if not html or not isinstance(html, str) or not html.strip():
            return True, "Empty or invalid response content"

        soup = soup or BeautifulSoup(html, "html.parser")
        
        # 1. Title tag check
        title = ""
        if soup.title:
            title_text = soup.title.get_text()
            if title_text:
                title = title_text.strip()
        
        title_lower = title.lower()
        
        # Common error indicators in page title
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
                # Avoid false positives for normal articles (usually have longer titles)
                if len(title) < 120:
                    return True, f"{label} detected in page title: '{title}'"
                    
        # Extract clean text from body to check for errors in text
        temp_soup = BeautifulSoup(html, "html.parser")
        for tag in temp_soup(["script", "style", "head", "iframe"]):
            tag.decompose()
        body_text = temp_soup.get_text()
        body_text = " ".join(body_text.split())
        body_text_lower = body_text.lower()
        
        # 2. Raw text error page check (e.g. plain text or simple page like "502 Bad Gateway")
        if len(body_text) < 1200:
            for pattern_lower, label in error_indicators:
                idx = body_text_lower.find(pattern_lower)
                if idx != -1 and idx < 120:
                    return True, f"{label} detected in short body text"
                elif len(body_text) < 250 and pattern_lower in body_text_lower:
                    return True, f"{label} detected in extremely short body text"
                    
        # 3. Cloudflare specific blocks/challenges
        if "cloudflare" in body_text_lower or "cloudflare" in title_lower:
            cf_signals = [
                "ray id", "security check", "ddos protection", "verify you are human",
                "checking your browser", "enable cookies", "enable javascript",
                "captcha", "turnstile", "unusual traffic", "browser validation"
            ]
            matched_signals = [sig for sig in cf_signals if sig in body_text_lower]
            if len(matched_signals) >= 2 or (len(body_text) < 1500 and len(matched_signals) >= 1):
                return True, f"Cloudflare block/challenge page detected (signals: {matched_signals})"
                
        # 4. Meta-refresh to challenge page (common Cloudflare interstitial pattern)
        meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if meta_refresh:
            content_attr = meta_refresh.get("content", "")
            if "url=" in content_attr.lower() and len(body_text) < 2000:
                return True, "Meta-refresh challenge page detected"

        return False, ""


    def is_allowed_by_robots(self, url: str) -> bool:
        """Checks if the path can be crawled according to robots.txt."""
        try:
            parsed_url = urllib.parse.urlparse(url)
            domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
            robots_url = f"{domain}/robots.txt"
            
            rp = self._get_robots_parser(domain, robots_url)
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            # Fallback to allowed in case of parsing exceptions
            return True

    def fetch_page(self, url: str, engine: str = "fast", ignore_robots: bool = False) -> str:
        """Fetches page content using HTTP requests or Selenium headless Chrome."""
        if not ignore_robots and not self.is_allowed_by_robots(url):
            raise PermissionError("Crawling forbidden by robots.txt")
            
        html_content = ""
        if engine == "dynamic" and SELENIUM_AVAILABLE:
            try:
                driver = self._get_selenium_driver()
                driver.get(url)
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.common.by import By
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                except Exception:
                    pass  # If body never appears, still grab whatever is there
                html_content = driver.page_source
            except Exception as e:
                from selenium.common.exceptions import TimeoutException
                if isinstance(e, TimeoutException):
                    print(f"Selenium page load timed out for {url}. Recreating driver.")
                    self.close()
                # Fallback to HTTP requests on selenium error
                print(f"Selenium fetch failed, falling back to HTTP: {e}")
                html_content = self._fetch_http(url)
        elif engine == "lightpanda" and LIGHTPANDA_AVAILABLE:
            try:
                html_content = self._fetch_lightpanda(url)
            except Exception as e:
                print(f"Lightpanda fetch failed, falling back to HTTP: {e}")
                html_content = self._fetch_http(url)
        elif engine == "lightpanda" and not LIGHTPANDA_AVAILABLE:
            print("Lightpanda not installed. Falling back to fast engine.")
            html_content = self._fetch_http(url)
        else:
            try:
                html_content = self._fetch_http(url)
            except requests.exceptions.HTTPError as http_err:
                status_code = http_err.response.status_code if http_err.response is not None else None
                # Only fall back to Selenium for 403 (e.g. Cloudflare / WAF block)
                if status_code == 403 and SELENIUM_AVAILABLE:
                    print(f"HTTP fetch returned 403 for {url}. Falling back to Selenium headless Chrome.")
                    try:
                        driver = self._get_selenium_driver()
                        driver.get(url)
                        from selenium.webdriver.support.ui import WebDriverWait
                        from selenium.webdriver.support import expected_conditions as EC
                        from selenium.webdriver.common.by import By
                        try:
                            WebDriverWait(driver, 10).until(
                                EC.presence_of_element_located((By.TAG_NAME, "body"))
                            )
                        except Exception:
                            pass
                        html_content = driver.page_source
                    except Exception as selenium_error:
                        from selenium.common.exceptions import TimeoutException
                        if isinstance(selenium_error, TimeoutException):
                            print(f"Selenium page load timed out for {url} during 403 fallback. Recreating driver.")
                            self.close()
                        print(f"Selenium fallback also failed: {selenium_error}")
                        raise http_err
                else:
                    raise http_err
            except Exception as e:
                # For connection timeouts, DNS failures, etc., fall back to Selenium if available
                if SELENIUM_AVAILABLE:
                    print(f"HTTP fetch failed for {url} ({e}). Falling back to Selenium headless Chrome.")
                    try:
                        driver = self._get_selenium_driver()
                        driver.get(url)
                        from selenium.webdriver.support.ui import WebDriverWait
                        from selenium.webdriver.support import expected_conditions as EC
                        from selenium.webdriver.common.by import By
                        try:
                            WebDriverWait(driver, 10).until(
                                EC.presence_of_element_located((By.TAG_NAME, "body"))
                            )
                        except Exception:
                            pass
                        html_content = driver.page_source
                    except Exception as selenium_error:
                        from selenium.common.exceptions import TimeoutException
                        if isinstance(selenium_error, TimeoutException):
                            print(f"Selenium page load timed out for {url} during connection fallback. Recreating driver.")
                            self.close()
                        print(f"Selenium fallback also failed: {selenium_error}")
                        raise e
                else:
                    raise e

        # Validate that the fetched content is not an error page
        is_err, reason = self.is_error_page(html_content)
        if is_err:
            raise RuntimeError(f"Crawl failed: {reason}")

        return html_content


    def _fetch_http(self, url: str) -> str:
        """
        Fetches page content using raw requests.
        For non-Chinese sites: behaviour identical to before (returns response.text).
        For Chinese sites (GB18030/GBK/GB2312): decodes bytes correctly instead of
        returning mojibake from requests' ISO-8859-1 fallback.
        """
        # Adaptive timeout: connect fast (5s), read generous (25s for slow servers)
        response = self.session.get(url, timeout=(5, 25), allow_redirects=True)
        response.raise_for_status()

        # ── Fast path: non-Chinese encoding detected ─────────────────────────────
        # requests correctly detects UTF-8, Big5, EUC-JP, etc. from the Content-Type
        # header. Return response.text directly — same as the current implementation.
        detected = (response.encoding or "").lower().replace("-", "").replace("_", "")
        _CN_NORMALIZED = {"gb2312", "gbk", "gb18030", "csgb2312", "chinese", "xgbk", "gbk2312"}

        if detected and detected not in _CN_NORMALIZED and detected not in ("iso88591", "windows1252", "latin1", ""):
            return response.text   # ← exits here for all non-Chinese, non-ambiguous sites

        # ── Slow path: ambiguous or Chinese encoding ─────────────────────────────
        # Reached only when: (a) requests guessed ISO-8859-1/latin-1 (common fallback
        # for missing charset), OR (b) Content-Type explicitly declares a CN charset.

        # Step 1: Check HTTP Content-Type header charset
        content_type = response.headers.get("Content-Type", "").lower()
        declared = ""
        if "charset=" in content_type:
            declared = content_type.split("charset=")[-1].split(";")[0].strip().lower().replace("-", "").replace("_", "")

        # Step 2: If not found or ambiguous, scan the first 4096 bytes for <meta charset>
        if not declared or declared in ("iso88591", "windows1252", "latin1", ""):
            import re as _re
            raw_preview = response.content[:4096].decode("latin-1", errors="replace")
            meta_match = _re.search(
                r'<meta[^>]+charset\s*=\s*["\']?\s*([a-zA-Z0-9_\-]+)',
                raw_preview,
                _re.IGNORECASE
            )
            if meta_match:
                declared = meta_match.group(1).strip().lower().replace("-", "").replace("_", "")

        # Step 3: If a Chinese charset is confirmed, decode from raw bytes using GB18030
        # (GB18030 is a strict superset of GBK and GB2312 — one codec handles all three)
        if declared in _CN_NORMALIZED:
            try:
                return response.content.decode("gb18030", errors="replace")
            except Exception:
                pass  # fall through to response.text

        # Step 4: Final fallback — response.text (same as current behaviour)
        return response.text

    @staticmethod
    def clean_html_content(soup: BeautifulSoup, html_content: str = "") -> str:
        """
        Extracts main article body text.
        PRIMARY: Trafilatura multi-algorithm extractor (Readability + jusText + custom).
        FALLBACK: Existing BeautifulSoup heuristic extractor.
        FALLBACK THRESHOLD: If Trafilatura returns fewer than MIN_EXTRACTED_SIZE characters.
        """
        # 1. Try Trafilatura first
        if html_content:
            try:
                from backend.firecrawl_converter import MIN_EXTRACTED_SIZE
                if _TRAFILATURA_AVAILABLE:
                    extracted = _trafilatura.extract(
                        html_content,
                        include_comments=False,
                        include_tables=True,
                        no_fallback=False,         # use all algorithms
                        favor_recall=True,         # prioritize content completeness over noise reduction
                        deduplicate=False,         # we handle deduplication ourselves
                    )
                    if extracted and len(extracted.strip()) >= MIN_EXTRACTED_SIZE:
                        return extracted.strip()
            except Exception as traf_err:
                print(f"[Trafilatura Warning] Extraction failed, using fallback: {traf_err}")

        # 2. Fall back to existing BeautifulSoup heuristic (keep existing code here unchanged)
        from backend.firecrawl_converter import extract_primary_content_container
        
        main_content = extract_primary_content_container(soup)
        # Reuse the already-extracted subtree directly instead of round-tripping through str()
        target_soup = main_content if hasattr(main_content, 'find_all') else BeautifulSoup(str(main_content), "html.parser")
            
        # Decompose non-content boilerplate elements
        for tag in target_soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
            
        def apply_stop_patterns(t_soup):
            stop_tag = None
            for tag in t_soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section"]):
                txt = tag.get_text().strip()
                if txt:
                    tag_name = tag.name.lower()
                    is_heading = tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]
                    if is_heading or len(txt) < 80:
                        is_stop = False
                        for pattern in STOP_PATTERNS:
                            if pattern.match(txt):
                                is_stop = True
                                break
                        if is_stop:
                            stop_tag = tag
                            break
            if stop_tag:
                to_decompose = list(stop_tag.next_elements)
                stop_tag.decompose()
                for el in to_decompose:
                    try:
                        el.decompose()
                    except Exception:
                        pass

        apply_stop_patterns(target_soup)
        text = target_soup.get_text(separator=" ")
        # Fallback to cleaning the full soup if article heuristic yielded extremely short text
        from backend.firecrawl_converter import MIN_EXTRACTED_SIZE
        if len(text.strip()) < MIN_EXTRACTED_SIZE:
            # Reuse the passed-in soup directly rather than re-parsing from string
            import copy
            target_soup = copy.copy(soup)
            for tag in target_soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                tag.decompose()
            apply_stop_patterns(target_soup)
            text = target_soup.get_text(separator=" ")
            
        return text

    @staticmethod
    def extract_author(soup: BeautifulSoup) -> Optional[str]:
        """Extracts the author of the page/news article from standard metadata tags or JSON-LD."""
        # 1. Try JSON-LD metadata
        for script in soup.find_all("script", type="application/ld+json"):
            if script.string:
                try:
                    import json
                    data = json.loads(script.string)
                    # Helper to traverse JSON for author
                    def find_author(obj):
                        if isinstance(obj, dict):
                            if obj.get("@type") == "NewsArticle" or obj.get("@type") == "Article":
                                author_obj = obj.get("author")
                                if isinstance(author_obj, dict) and author_obj.get("name"):
                                    return str(author_obj.get("name")).strip()
                                elif isinstance(author_obj, list) and len(author_obj) > 0:
                                    first = author_obj[0]
                                    if isinstance(first, dict) and first.get("name"):
                                        return str(first.get("name")).strip()
                                    elif isinstance(first, str):
                                        return first.strip()
                                elif isinstance(author_obj, str):
                                    return author_obj.strip()
                            for k, v in obj.items():
                                res = find_author(v)
                                if res:
                                    return res
                        elif isinstance(obj, list):
                            for item in obj:
                                res = find_author(item)
                                if res:
                                    return res
                        return None
                    author = find_author(data)
                    if author:
                        return author
                except Exception:
                    pass

        # 2. Try standard Meta tags
        author_selectors = [
            ("meta", {"name": "author"}),
            ("meta", {"property": "article:author"}),
            ("meta", {"name": "twitter:creator"}),
            ("meta", {"property": "og:site_name"})  # Fallback to site name if author is missing
        ]
        for tag_name, attrs in author_selectors:
            tag = soup.find(tag_name, attrs=attrs)
            if tag and tag.get("content"):
                return str(tag.get("content")).strip()
                
        # 3. Try inline author elements
        author_elements = [
            soup.find(class_=re.compile("author|byline|writer", re.I)),
            soup.find(id=re.compile("author|byline|writer", re.I))
        ]
        for element in author_elements:
            if element:
                text = element.get_text().strip()
                # Clean prefix words like "By " or "Posted by "
                text_cleaned = re.sub(r'(?i)^(?:by|posted\s+by)\s+', '', text)
                if 0 < len(text_cleaned) < 100:
                    return text_cleaned
                    
        return None

    @staticmethod
    def extract_image_url(soup: BeautifulSoup, page_url: str = "") -> Optional[str]:
        """Extracts the lead article or OG image URL from the page."""
        image_selectors = [
            ("meta", {"property": "og:image"}),
            ("meta", {"name": "twitter:image"}),
            ("link", {"rel": "image_src"})
        ]
        for tag_name, attrs in image_selectors:
            tag = soup.find(tag_name, attrs=attrs)
            val = tag.get("content") or tag.get("href") if tag else None
            if val:
                # Resolve relative URLs
                if page_url:
                    val = urllib.parse.urljoin(page_url, val.strip())
                return val.strip()
                
        # Fallback to first large image in body
        for img in soup.find_all("img"):
            src = img.get("src")
            if src and not any(ext in src.lower() for ext in [".gif", "logo", "icon", "avatar"]):
                if page_url:
                    src = urllib.parse.urljoin(page_url, src.strip())
                return src.strip()
                
        return None

    @staticmethod
    def detect_language(soup: BeautifulSoup, body_text: str = "") -> Optional[str]:
        """
        Detects page language.
        Priority: HTML lang tag > Trafilatura/py3langid > langdetect fallback.
        """
        # 1. HTML lang tag
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            return html_tag.get("lang").split("-")[0].strip().lower()
            
        meta_lang = soup.find("meta", attrs={"http-equiv": "content-language"})
        if meta_lang and meta_lang.get("content"):
            return meta_lang.get("content").split(",")[0].strip().lower()

        # 2. Trafilatura/py3langid (new — more accurate on short texts)
        if body_text:
            try:
                if _PY3LANGID_AVAILABLE:
                    lang, confidence = _py3langid.classify(body_text[:2000])
                    if confidence > 0.9:
                        return lang
            except Exception:
                pass
            
        # 3. langdetect fallback
        if body_text:
            try:
                from langdetect import detect
                return detect(body_text[:2000])
            except Exception:
                pass
        return None

    @staticmethod
    def detect_date(soup: BeautifulSoup) -> Optional[datetime]:
        """Detects publication or last updated date from page meta tags."""
        date_selectors = [
            ("meta", {"property": "article:published_time"}),
            ("meta", {"property": "og:updated_time"}),
            ("meta", {"name": "pubdate"}),
            ("meta", {"name": "date"}),
            ("meta", {"name": "last-modified"}),
            ("time", {"datetime": True})
        ]
        
        for tag_name, attrs in date_selectors:
            tag = soup.find(tag_name, attrs=attrs)
            if tag:
                val = tag.get("datetime") or tag.get("content")
                if val:
                    try:
                        # BUGFIX: Return timezone-aware datetime to avoid offset-naive vs offset-aware comparison errors.
                        val_cleaned = val.split("T")[0]  # Take YYYY-MM-DD
                        return datetime.strptime(val_cleaned, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
        return None

    @staticmethod
    def calculate_content_hash(text: str) -> str:
        """Generates MD5 hash of normalized text for duplicate detection."""
        normalized = " ".join(text.lower().split())
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def generate_snippet(self, text: str, terms: Set[str], context_words: int = 20) -> str:
        """Generates a snippet highlighting the keyword."""
        # Find first term occurrence
        normalized_text = " ".join(text.split())
        lower_text = normalized_text.lower()
        
        first_idx = -1
        matched_term = ""
        for term in terms:
            idx = lower_text.find(term.lower())
            if idx != -1 and (first_idx == -1 or idx < first_idx):
                first_idx = idx
                matched_term = term
                
        if first_idx == -1:
            return normalized_text[:150] + "..." if len(normalized_text) > 150 else normalized_text
            
        # Extract surrounding context
        words = normalized_text.split()
        lower_words = [w.lower() for w in words]
        
        # Find which word index contains the match
        match_word_idx = 0
        for i, w in enumerate(lower_words):
            if matched_term.lower() in w:
                match_word_idx = i
                break
                
        start = max(0, match_word_idx - context_words // 2)
        end = min(len(words), match_word_idx + context_words // 2)
        
        snippet = " ".join(words[start:end])
        if start > 0:
            snippet = "... " + snippet
        if end < len(words):
            snippet = snippet + " ..."
            
        return snippet

    def evaluate_boolean_query(self, text: str, query: str, case_sensitive: bool = False) -> bool:
        """
        Parses and evaluates a Boolean search expression on text using a recursive descent parser.
        Supports AND, OR, NOT, and parentheses.
        """
        import re as _re
        def tokenize(q):
            q = q.replace("(", " ( ").replace(")", " ) ")
            return _re.findall(r'\(|\)|"[^"]+"|\bAND\b|\bOR\b|\bNOT\b|\S+', q, _re.IGNORECASE)
        def term_matches(term):
            t = term.strip('"')
            return t in text if case_sensitive else t.lower() in text.lower()
        tokens = tokenize(query)
        pos = [0]
        def peek(): return tokens[pos[0]] if pos[0] < len(tokens) else None
        def consume():
            tok = tokens[pos[0]]; pos[0] += 1; return tok
        def parse_expr(): return parse_or()
        def parse_or():
            left = parse_and()
            while peek() and peek().upper() == "OR": consume(); right = parse_and(); left = left or right
            return left
        def parse_and():
            left = parse_not()
            while peek() and peek().upper() == "AND": consume(); right = parse_not(); left = left and right
            return left
        def parse_not():
            if peek() and peek().upper() == "NOT": consume(); return not parse_atom()
            return parse_atom()
        def parse_atom():
            tok = peek()
            if tok == "(": consume(); val = parse_expr(); consume() if peek() == ")" else None; return val
            if tok is not None: consume(); return term_matches(tok)
            return False
        try: return parse_expr()
        except Exception: return False

    def analyze_page(
        self,
        html_content: str,
        url: str,
        keyword: str,
        match_type: str = "phrase",
        case_sensitive: bool = False,
        exact_match: bool = False
    ) -> Dict[str, Any]:
        """
        Analyzes page contents for keyword matches, generates statistics,
        snippet, metadata, and calculates relevance score.
        """
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 1. Gather page metadata
        title = ""
        if soup.title:
            title_text = soup.title.get_text()
            if title_text:
                title = title_text.strip()
        
        meta_desc_tag = (
            soup.find("meta", attrs={"name": "description"}) or 
            soup.find("meta", attrs={"property": "og:description"})
        )
        description = ""
        if meta_desc_tag:
            desc_content = meta_desc_tag.get("content")
            if desc_content:
                if isinstance(desc_content, list):
                    desc_content = " ".join(desc_content)
                description = str(desc_content).strip()
        
        from backend.firecrawl_converter import convert_html_to_firecrawl_schema
        normalized_data = convert_html_to_firecrawl_schema(html_content, url, soup=soup)
        markdown_content = normalized_data["data"]["markdown"]
        
        body_text = self.clean_html_content(soup, html_content=html_content)
        language = self.detect_language(soup, body_text)
        pub_date = self.detect_date(soup)
        content_hash = self.calculate_content_hash(body_text)
        author = self.extract_author(soup)
        image_url = self.extract_image_url(soup, url)

        # Enrich metadata using Trafilatura if possible
        try:
            if _TRAFILATURA_AVAILABLE:
                traf_meta = _traf_extract_metadata(html_content)
            else:
                traf_meta = None
            if traf_meta:
                # Enrich author if existing extraction returned "Unknown"
                if (not author or author == "Unknown") and traf_meta.author:
                    author = str(traf_meta.author)[:100]
                # Enrich title if empty
                if not title and traf_meta.title:
                    title = str(traf_meta.title)[:200]
                # Enrich description if empty
                if not description and traf_meta.description:
                    description = str(traf_meta.description)
                # Enrich pub_date if not found by existing parser
                if not pub_date and traf_meta.date:
                    try:
                        # BUGFIX: Return timezone-aware datetime to avoid offset-naive vs offset-aware comparison errors.
                        pub_date = datetime.strptime(str(traf_meta.date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
        except Exception:
            pass  # Metadata enrichment is best-effort, never block the pipeline

        # Compute SimHash fingerprint
        from backend.simhash_dedup import compute_simhash
        simhash_val = compute_simhash(body_text)

        
        # 2. Extract domain and check URL keyword presence
        parsed_url = urllib.parse.urlparse(url)
        domain = parsed_url.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
            
        # 3. Analyze Keyword Match
        matched = False
        total_occurrences = 0
        found_in_title = False
        found_in_description = False
        found_in_body = False
        found_in_url = False
        
        # Prepare list of search terms
        is_keyword_free = not keyword or not keyword.strip()
        
        search_terms_list = []
        if not is_keyword_free:
            try:
                # Check if keyword is a JSON list
                parsed_json = json.loads(keyword)
                if isinstance(parsed_json, list):
                    search_terms_list = [str(k).strip() for k in parsed_json if str(k).strip()]
                else:
                    search_terms_list = [str(parsed_json).strip()]
            except Exception:
                # If not JSON, check if it's comma-separated or newline-separated
                if "," in keyword or "\n" in keyword:
                    search_terms_list = [k.strip() for k in re.split(r'[,\n]', keyword) if k.strip()]
                else:
                    search_terms_list = [keyword.strip()]
            search_terms_list = list(dict.fromkeys(search_terms_list))

        # Check locations and count occurrences
        if is_keyword_free:
            matched = True
            total_occurrences = 0
            found_in_title = False
            found_in_description = False
            found_in_body = False
            found_in_url = False
            matched_keywords_found = []
            search_terms = set()
        else:
            search_terms = set(search_terms_list)
            if match_type == "boolean":
                # Extract plain terms from boolean expression (words/phrases in quotes or alphanumeric)
                search_terms = set(re.findall(r'"([^"]+)"|(\b\w+\b)', keyword))
                # Flatten tuples from findall
                search_terms = {t[0] or t[1] for t in search_terms if t[0] or t[1]}
                search_terms = {t for t in search_terms if t.upper() not in ("AND", "OR", "NOT")}
                
            # Count occurrences in each location
            def count_occurrences(text_content: str, terms: Set[str]) -> int:
                count = 0
                for term in terms:
                    if exact_match:
                        # Match exact words using regex word boundaries
                        pattern = rf"\b{re.escape(term)}\b"
                        flags = 0 if case_sensitive else re.IGNORECASE
                        count += len(re.findall(pattern, text_content, flags))
                    else:
                        # Match substrings
                        if case_sensitive:
                            count += text_content.count(term)
                        else:
                            count += text_content.lower().count(term.lower())
                return count

            found_in_url = count_occurrences(url, search_terms) > 0
            
            title_count = count_occurrences(title, search_terms)
            found_in_title = title_count > 0
            
            desc_count = count_occurrences(description, search_terms)
            found_in_description = desc_count > 0
            
            body_count = count_occurrences(body_text, search_terms)
            found_in_body = body_count > 0
            
            total_occurrences = title_count + desc_count + body_count + (1 if found_in_url else 0)

            # Track which specific keywords matched
            matched_keywords_found = []
            for term in (search_terms if match_type == "boolean" else search_terms_list):
                term_set = {term}
                if (count_occurrences(url, term_set) > 0 or 
                    count_occurrences(title, term_set) > 0 or 
                    count_occurrences(description, term_set) > 0 or 
                    count_occurrences(body_text, term_set) > 0):
                    matched_keywords_found.append(term)
            
            # Evaluate boolean query matching if match_type is boolean
            if match_type == "boolean":
                # We check the entire full text combining title, description, body, url
                full_crawlable_text = f"{title}\n{description}\n{body_text}\n{url}"
                matched = self.evaluate_boolean_query(full_crawlable_text, keyword, case_sensitive)
            else:
                # Require that at least one searched keyword is found in the parsed content (OR logic)
                matched = len(matched_keywords_found) > 0


        # 4. Snippet Generation
        snippet = ""
        if matched:
            if is_keyword_free:
                normalized_text = " ".join(body_text.split())
                snippet = normalized_text[:150] + "..." if len(normalized_text) > 150 else normalized_text
            else:
                snippet = self.generate_snippet(body_text, search_terms)
            
        # 5. Relevance Scoring (0-100)
        relevance_score = 0.0
        if matched:
            if is_keyword_free:
                relevance_score = 100.0
            else:
                # Weights: Title (35pts), Description (15pts), URL (10pts), Body density (40pts)
                if found_in_title:
                    relevance_score += 35
                if found_in_description:
                    relevance_score += 15
                if found_in_url:
                    relevance_score += 10
                    
                # Density score (up to 40pts)
                words = body_text.split()
                word_count = len(words)
                if word_count > 0 and body_count > 0:
                    density = body_count / word_count
                    # Peak density is 2% = full 40 points
                    density_score = min(40.0, density * 2000.0)
                    relevance_score += density_score
                    
                relevance_score = round(relevance_score, 1)

        # 6. Extract full images and videos list
        images_list = normalized_data.get("data", {}).get("images", [])
        videos_list = normalized_data.get("data", {}).get("videos", [])
        
        image_links_json = json.dumps([img["src"] for img in images_list if img.get("src")])
        video_links_json = json.dumps([v["src"] for v in videos_list if v.get("src")])

        return {
            "title": title[:200] if title else "Untitled",
            "snippet": snippet,
            "occurrences": total_occurrences,
            "found_in_title": found_in_title,
            "found_in_description": found_in_description,
            "found_in_body": found_in_body,
            "found_in_url": found_in_url,
            "language": language,
            "discovered_at": pub_date or datetime.now(timezone.utc),
            "domain": domain,
            "content_hash": content_hash,
            "description": description,
            "full_content": markdown_content,
            "raw_html": html_content,
            "author": author[:100] if author else "Unknown",
            "simhash": simhash_val,

            "image_url": image_url,
            "image_links": image_links_json,
            "video_links": video_links_json,
            "relevance_score": relevance_score,
            "matched": matched,
            "matched_keywords": json.dumps(matched_keywords_found)
        }
