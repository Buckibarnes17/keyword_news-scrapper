"""
tor_setup.py — Auto-downloader and setup utility for Tor Expert Bundle on Windows
"""

import os
import sys
import tarfile
import urllib.request
import logging

logger = logging.getLogger("keywordscout.tor_setup")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOR_URL = "https://archive.torproject.org/tor-package-archive/torbrowser/15.0.17/tor-expert-bundle-windows-x86_64-15.0.17.tar.gz"
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOR_DIR = os.path.join(WORKSPACE_DIR, "backend", "tor")
TOR_EXE_PATH = os.path.join(TOR_DIR, "tor", "tor.exe")

def download_and_extract_tor() -> str:
    """
    Downloads the official Tor Expert Bundle for Windows and extracts it inside backend/tor/
    Returns the absolute path to tor.exe.
    """
    if os.path.exists(TOR_EXE_PATH):
        logger.info("[Tor Setup] Tor executable already exists: %s", TOR_EXE_PATH)
        return TOR_EXE_PATH

    os.makedirs(TOR_DIR, exist_ok=True)
    temp_archive = os.path.join(TOR_DIR, "tor-expert-bundle.tar.gz")

    logger.info("[Tor Setup] Downloading Tor Expert Bundle from %s...", TOR_URL)
    try:
        urllib.request.urlretrieve(TOR_URL, temp_archive)
        logger.info("[Tor Setup] Download complete. Extracting archive...")
        
        with tarfile.open(temp_archive, "r:gz") as tar:
            tar.extractall(path=TOR_DIR)
        
        logger.info("[Tor Setup] Extraction complete.")
        
        # Clean up temporary download file
        if os.path.exists(temp_archive):
            os.remove(temp_archive)
            
        if os.path.exists(TOR_EXE_PATH):
            logger.info("[Tor Setup] Tor successfully set up: %s", TOR_EXE_PATH)
            return TOR_EXE_PATH
        else:
            raise FileNotFoundError(f"Could not find tor.exe at expected path: {TOR_EXE_PATH}")
            
    except Exception as e:
        logger.error("[Tor Setup] Failed to setup Tor: %s", e)
        if os.path.exists(temp_archive):
            try: os.remove(temp_archive)
            except Exception: pass
        raise e

if __name__ == "__main__":
    try:
        download_and_extract_tor()
    except Exception as err:
        sys.exit(1)
