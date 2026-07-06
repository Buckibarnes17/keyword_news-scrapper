"""
simhash_dedup.py
Self-contained SimHash near-duplicate detection engine.
Ported from Trafilatura (Apache-2.0) for KeywordScout v2.0.
Uses Blake2b token hashing + Charikar SimHash (64-bit).
Hamming distance threshold: <= 3 bits different = near-duplicate.

Dependencies:
    - hashlib (stdlib)
    - functools (stdlib)
    - string (stdlib)
    - unicodedata (stdlib)
    - operator (stdlib)
"""

import string
import unicodedata
from functools import lru_cache
from hashlib import blake2b
from operator import add

PUNCT_TBL = str.maketrans({i: " " for i in range(0x10FFFF) if unicodedata.category(chr(i))[0] == "P"})

def _tokenize(text: str, length: int = 64) -> list:
    clean = text.translate(PUNCT_TBL)
    tokens = [t for t in clean.split() if t.isalnum()]
    # Sample tokens: prefer longer tokens, take up to `length` tokens
    for min_len in range(4, -1, -1):
        sample = [t for t in tokens if len(t) > min_len]
        if len(sample) >= length // 2:
            return sample
    return tokens

@lru_cache(maxsize=2**14)
def _token_vector(token: str, length: int = 64) -> tuple:
    h = int.from_bytes(blake2b(token.encode(), digest_size=8).digest(), "big")
    return tuple(1 if h & (1 << i) else -1 for i in range(length))

def compute_simhash(text: str, length: int = 64) -> str:
    """Compute a SimHash fingerprint for text. Returns hex string."""
    if not text or len(text.strip()) < 30:
        return ""
    tokens = _tokenize(text, length)
    if not tokens:
        return ""
    vector = [0] * length
    for token in tokens:
        vec = _token_vector(token, length)
        vector = list(map(add, vector, vec))
    h = sum(1 << i for i in range(length) if vector[i] >= 0)
    return hex(h)[2:]

def hamming_distance(h1: str, h2: str) -> int:
    """Compute Hamming distance between two hex SimHash strings."""
    try:
        xor = int(h1, 16) ^ int(h2, 16)
        # Count differing bits using bin count
        return bin(xor).count("1")
    except (ValueError, TypeError):
        return 999  # treat as non-duplicate if hashes are invalid

def is_near_duplicate(h1: str, h2: str, threshold: int = 3) -> bool:
    """Returns True if two SimHash hex strings are near-duplicates (Hamming <= threshold)."""
    if not h1 or not h2:
        return False
    return hamming_distance(h1, h2) <= threshold
