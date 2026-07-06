"""
tor_router.py — Tor SOCKS5 routing utilities for KeywordScout v2.0

Provides:
  - TOR_SOCKS5_URL: the canonical proxy string for requests/httpx
  - TOR_SOCKS5_CHROME: the proxy string Chrome flags accept (no trailing 'h')
  - check_tor_reachability(): validates the local Tor daemon is up before a job starts
  - TOR_REQUESTS_PROXIES: requests-compatible proxies dict
  - TOR_PLAYWRIGHT_PROXY: Playwright-compatible proxy dict
  - is_tor_proxy_url(): detect if a proxy_url is the Tor address
  - resolve_effective_proxy(): merge use_tor flag with proxy_url field

The Tor daemon is NEVER managed by this module. It must already be running
on 127.0.0.1:9050 (standard Tor Browser or `tor` system service).

DNS leak prevention:
  - requests:    socks5h:// routes DNS through Tor automatically (the 'h' suffix)
  - Chrome:      --proxy-server=socks5://... + --host-resolver-rules prevents
                 local DNS resolution leaking outside Tor
  - Playwright:  proxy server string — Playwright handles DNS via the proxy
"""

import socket
import logging

logger = logging.getLogger("keywordscout.tor_router")

# ── Constants ──────────────────────────────────────────────────────────────────
TOR_HOST = "127.0.0.1"
TOR_PORT = 9050

# socks5h:// — the 'h' tells PySocks to resolve hostnames through Tor
# This prevents DNS leaks on requests/urllib3 transports
TOR_SOCKS5_URL = f"socks5h://{TOR_HOST}:{TOR_PORT}"

# Chrome --proxy-server flag only accepts socks5:// (no 'h')
# DNS is handled separately via --host-resolver-rules
TOR_SOCKS5_CHROME = f"socks5://{TOR_HOST}:{TOR_PORT}"

# Playwright proxy dict
TOR_PLAYWRIGHT_PROXY = {"server": f"socks5://{TOR_HOST}:{TOR_PORT}"}

# requests-compatible proxies dict — pass directly as proxies= kwarg
TOR_REQUESTS_PROXIES = {
    "http":  TOR_SOCKS5_URL,
    "https": TOR_SOCKS5_URL,
}


def check_tor_reachability(timeout: float = 5.0) -> dict:
    """
    Validates that the Tor SOCKS5 proxy is accepting connections on 127.0.0.1:9050.

    Returns:
        {"reachable": True,  "message": "Tor SOCKS5 proxy is reachable on port 9050"}
        {"reachable": False, "message": "<reason>"}

    Does NOT verify Tor has built a circuit — only that the port is open.
    Circuit readiness is implied after ~10-15 seconds of Tor startup.
    """
    try:
        sock = socket.create_connection((TOR_HOST, TOR_PORT), timeout=timeout)
        sock.close()
        logger.info("[Tor] SOCKS5 proxy reachable on %s:%d", TOR_HOST, TOR_PORT)
        return {
            "reachable": True,
            "message": f"Tor SOCKS5 proxy is reachable on {TOR_HOST}:{TOR_PORT}"
        }
    except ConnectionRefusedError:
        msg = (
            f"Tor SOCKS5 proxy is NOT running on {TOR_HOST}:{TOR_PORT}. "
            "Start Tor Browser or run: sudo systemctl start tor"
        )
        logger.warning("[Tor] %s", msg)
        return {"reachable": False, "message": msg}
    except socket.timeout:
        msg = (
            f"Connection to Tor SOCKS5 proxy at {TOR_HOST}:{TOR_PORT} timed out. "
            "Tor may be starting up — wait 15 seconds and try again."
        )
        logger.warning("[Tor] %s", msg)
        return {"reachable": False, "message": msg}
    except OSError as e:
        msg = f"Cannot reach Tor SOCKS5 proxy: {e}"
        logger.warning("[Tor] %s", msg)
        return {"reachable": False, "message": msg}


def is_tor_proxy_url(proxy_url: str) -> bool:
    """Returns True if the given proxy_url is the local Tor SOCKS5 address."""
    if not proxy_url:
        return False
    return TOR_HOST in proxy_url and str(TOR_PORT) in proxy_url


def resolve_effective_proxy(proxy_url: str, use_tor: bool) -> str:
    """
    Determines the effective proxy URL for a job.

    Priority:
      1. use_tor=True  → always return TOR_SOCKS5_URL, regardless of proxy_url
      2. use_tor=False, proxy_url set → return proxy_url as-is
      3. use_tor=False, no proxy_url  → return empty string (no proxy)

    Logs a warning if use_tor=True overrides a manually set non-Tor proxy_url.
    """
    if use_tor:
        if proxy_url and proxy_url.strip() and not is_tor_proxy_url(proxy_url):
            logger.warning(
                "[Tor] use_tor=True overrides manually set proxy_url='%s'. "
                "Tor SOCKS5 will be used for this job instead.",
                proxy_url
            )
        return TOR_SOCKS5_URL
    return proxy_url or ""
