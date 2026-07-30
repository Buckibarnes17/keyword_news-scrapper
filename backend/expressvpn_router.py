"""
expressvpn_router.py — ExpressVPN routing utilities for KeywordScout v2.0

Provides:
  - connect_singapore(): connects system to Singapore VPN exit node
  - verify_singapore_ip(): verifies through external API that current IP is Singapore
  - disconnect(): disconnects from ExpressVPN
  - verify_normal_ip(): verifies IP has reverted to non-Singapore/original address
  - VPNLockContext: context manager to lock/serialize VPN access and ensure cleanup
"""

import os
import subprocess
import time
import requests
import logging
import threading

logger = logging.getLogger("keywordscout.expressvpn_router")

# Global lock to serialize VPN access across crawl runs
vpn_lock = threading.Lock()

# Cached original public IP (before connecting)
_normal_ip = None

# Default Singapore region (from expressvpnctl get regions)
DEFAULT_REGION = "singapore-cbd"


def get_expressvpn_path() -> str:
    """Finds the absolute path to the expressvpnctl executable on Windows."""
    paths = [
        r"C:\Program Files\ExpressVPN\expressvpnctl.exe",
        r"C:\Program Files (x86)\ExpressVPN\expressvpnctl.exe"
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return "expressvpnctl"


def run_expressvpn_cmd(args: list) -> tuple:
    """Runs the expressvpnctl command line interface. Returns (exit_code, stdout, stderr)."""
    cli = get_expressvpn_path()
    cmd = [cli] + args
    try:
        # 15 seconds timeout to avoid hanging indefinitely if daemon doesn't respond
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        logger.error(f"[ExpressVPN] Command timed out: {cmd}")
        return -1, "", "Timeout expired"
    except Exception as e:
        logger.error(f"[ExpressVPN] Failed to run command {cmd}: {e}")
        return -1, "", str(e)


def get_connection_state() -> str:
    """Returns the current connection state ('Connected', 'Disconnected', 'Connecting', etc.)."""
    code, stdout, stderr = run_expressvpn_cmd(["get", "connectionstate"])
    if code == 0:
        return stdout.strip()
    
    # Fallback status parse
    code, stdout, stderr = run_expressvpn_cmd(["status"])
    if code == 0:
        if "Connected" in stdout:
            return "Connected"
        elif "Connecting" in stdout:
            return "Connecting"
    return "Disconnected"


def get_current_ip_info() -> dict:
    """
    Retrieves public IP details from ipinfo.io with fallback to ip-api.com.
    Returns: {"ip": str, "country_code": str} (country_code is uppercase 2-letters, e.g. 'SG').
    """
    # 1. Try ipinfo.io
    try:
        resp = requests.get("https://ipinfo.io/json", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            ip = data.get("ip")
            country = data.get("country", "").upper()
            if ip and country:
                return {"ip": ip, "country_code": country}
    except Exception as e:
        logger.warning(f"[ExpressVPN] ipinfo.io fetch failed: {e}")

    # 2. Try ip-api.com
    try:
        resp = requests.get("http://ip-api.com/json/", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            ip = data.get("query")
            country = data.get("countryCode", "").upper()
            if ip and country:
                return {"ip": ip, "country_code": country}
    except Exception as e:
        logger.error(f"[ExpressVPN] ip-api.com fetch failed: {e}")

    raise RuntimeError("Unable to verify public IP via geolocation services (network down or rate limited).")


def connect_singapore(location: str = DEFAULT_REGION, timeout: int = 30) -> None:
    """
    Connects system traffic to Singapore VPN.
    Saves the original public IP address before connection.
    Polls state until 'Connected'. Raises RuntimeError on failure.
    """
    global _normal_ip
    
    # Pre-cache normal IP if not already set and currently disconnected
    state = get_connection_state()
    if state == "Disconnected":
        try:
            info = get_current_ip_info()
            _normal_ip = info["ip"]
            logger.info(f"[ExpressVPN] Cached original normal IP: {_normal_ip}")
        except Exception as e:
            logger.warning(f"[ExpressVPN] Could not pre-cache normal IP: {e}")

    logger.info(f"[ExpressVPN] Connecting to location: {location} ...")
    code, stdout, stderr = run_expressvpn_cmd(["connect", location])
    if code != 0:
        err_msg = (stderr or stdout or "")
        if "background enable" in err_msg:
            logger.warning("[ExpressVPN] Daemon connection failure. Attempting to enable background service...")
            run_expressvpn_cmd(["background", "enable"])
            time.sleep(2)  # Give the daemon a moment to initialize
            code, stdout, stderr = run_expressvpn_cmd(["connect", location])
            
        if code != 0:
            raise RuntimeError(f"Failed to initiate connection: {stderr or stdout}")

    # Poll connection state
    start_time = time.time()
    while time.time() - start_time < timeout:
        curr_state = get_connection_state()
        if curr_state == "Connected":
            logger.info("[ExpressVPN] CLI reports Connected state.")
            return
        elif curr_state in ("Connecting", "Reconnecting"):
            time.sleep(1)
        else:
            time.sleep(1)

    raise TimeoutError(f"ExpressVPN did not connect to '{location}' within {timeout} seconds.")


def verify_singapore_ip() -> None:
    """
    Verifies that the apparent public IP matches the Singapore ('SG') country code.
    Raises RuntimeError if verification fails.
    """
    logger.info("[ExpressVPN] Verifying routing Exit Node...")
    try:
        info = get_current_ip_info()
        ip = info["ip"]
        country = info["country_code"]
        logger.info(f"[ExpressVPN] Current IP: {ip}, Country: {country}")
        if country != "SG":
            raise RuntimeError(f"Expected country 'SG' but got '{country}'.")
        logger.info("[ExpressVPN] Exit Node Geolocation verified successfully as Singapore.")
    except Exception as e:
        try:
            state = get_connection_state()
        except Exception:
            state = "Unknown"
        
        if state == "Connected":
            logger.warning(f"[ExpressVPN] Geolocation check failed ({e}), but CLI reports Connected. Proceeding anyway.")
        else:
            raise RuntimeError(f"Singapore Geolocation routing check failed: {e}")


def disconnect(timeout: int = 30) -> None:
    """
    Disconnects ExpressVPN.
    Polls state until 'Disconnected'. Raises RuntimeError on failure.
    """
    logger.info("[ExpressVPN] Disconnecting...")
    code, stdout, stderr = run_expressvpn_cmd(["disconnect"])
    if code != 0:
        raise RuntimeError(f"Failed to initiate disconnect: {stderr or stdout}")

    start_time = time.time()
    while time.time() - start_time < timeout:
        curr_state = get_connection_state()
        if curr_state == "Disconnected":
            logger.info("[ExpressVPN] CLI reports Disconnected state.")
            return
        elif curr_state in ("Disconnecting", "Connected"):
            time.sleep(1)
        else:
            time.sleep(1)

    raise TimeoutError(f"ExpressVPN did not disconnect within {timeout} seconds.")


def verify_normal_ip() -> None:
    """
    Verifies the public IP is no longer routed to Singapore.
    Verifies against the cached pre-VPN IP, or falls back to verifying country is not SG.
    """
    global _normal_ip
    logger.info("[ExpressVPN] Verifying return to normal routing...")
    try:
        info = get_current_ip_info()
        ip = info["ip"]
        country = info["country_code"]
        logger.info(f"[ExpressVPN] Current IP: {ip}, Country: {country}")

        if _normal_ip and ip == _normal_ip:
            logger.info("[ExpressVPN] Return verification passed: IP reverted to cached normal IP.")
            return

        if country == "SG":
            raise RuntimeError("Apparent location is still Singapore ('SG').")

        logger.info(f"[ExpressVPN] Return verification passed: IP country code is '{country}' (non-Singapore).")
    except Exception as e:
        try:
            state = get_connection_state()
        except Exception:
            state = "Unknown"

        if state == "Disconnected":
            logger.warning(f"[ExpressVPN] Normal IP routing revert check failed ({e}), but CLI reports Disconnected. Proceeding anyway.")
        else:
            raise RuntimeError(f"Normal IP routing revert check failed: {e}")


class VPNLockContext:
    """
    Context manager to serialized VPN routing.
    Ensures safe lock acquisition, release, and guaranteed disconnection.
    """
    def __init__(self, location: str = DEFAULT_REGION, timeout: int = 30):
        self.location = location
        self.timeout = timeout

    def __enter__(self):
        logger.info("[VPNLockContext] Waiting to acquire global VPN Lock...")
        vpn_lock.acquire()
        logger.info("[VPNLockContext] VPN Lock acquired.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            # Clean up: disconnect VPN if it was left active
            state = get_connection_state()
            if state in ("Connected", "Connecting", "Reconnecting"):
                logger.info("[VPNLockContext] VPN is active in cleanup block. Disconnecting...")
                try:
                    disconnect(self.timeout)
                    verify_normal_ip()
                except Exception as ex:
                    logger.error(f"[VPNLockContext] Cleanup disconnect failed: {ex}")
        finally:
            vpn_lock.release()
            logger.info("[VPNLockContext] VPN Lock released.")
