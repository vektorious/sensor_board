"""API-key hashing.

Keys are never stored or logged in plaintext. We keep a SHA-256 hash alongside
each measurement so usage can be attributed and a tester's data bulk-deleted by
hash — see the "Beta testing" section of the README.
"""
import hashlib


def hash_api_key(key: str) -> str:
    """SHA-256 hex digest of an API key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_hash_prefix(key: str, length: int = 12) -> str:
    """Short hash prefix, safe for logs."""
    return hash_api_key(key)[:length]
