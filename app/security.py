"""Credential hashing, constant-time comparison, and log sanitising.

No secret is ever stored or logged in plaintext (plan §14, §20). Two kinds of
credential exist and they are deliberately different things:

* **API keys** are configured by the operator. They grant a *policy* — higher
  limits and persistent devices — and nothing else.
* **Write keys** are chosen by the client and identify who owns a device ID.
  They are never generated, returned, or recoverable server-side.

Both are compared as SHA-256 digests using `hmac.compare_digest`, so a wrong
guess takes the same time as a near-miss and leaks nothing about the stored
value. SHA-256 rather than a password hash is a considered choice: these are
high-entropy machine credentials submitted on every measurement, not
user-chosen passwords, so per-request bcrypt/argon2 cost would buy little and
would put a CPU-bound operation on the hot ingestion path.
"""
import hashlib
import hmac

# Log fields sourced from user input are truncated to this many characters, so
# a long device ID cannot flood the log or push context off the line.
_MAX_LOG_FIELD = 80


def hash_secret(value: str) -> str:
    """SHA-256 hex digest of a credential (API key or write key)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# Kept as a distinct name because the call sites read better, and because API
# keys and write keys may want different treatment later.
hash_api_key = hash_secret
hash_write_key = hash_secret


def key_hash_prefix(key: str, length: int = 12) -> str:
    """Short hash prefix, safe for logs — enough to correlate, not to reverse."""
    return hash_secret(key)[:length]


def matches(candidate: str, stored_hash: str | None) -> bool:
    """Constant-time check of a plaintext credential against a stored hash."""
    if not stored_hash or candidate is None:
        return False
    return hmac.compare_digest(hash_secret(candidate), stored_hash)


def matches_any(candidate: str | None, valid: list[str]) -> bool:
    """Constant-time membership test against a list of plaintext credentials.

    Every entry is compared even after a match is found, so the time taken
    reveals neither which key matched nor how many are configured.
    """
    if candidate is None:
        return False
    found = False
    for known in valid:
        if hmac.compare_digest(candidate, known):
            found = True
    return found


def hash_ip(ip: str | None) -> str:
    """Stable pseudonym for a client address.

    Device-creation limits only ever ask "is this the same address as before?",
    which a hash answers, so the address itself never has to be stored.
    """
    return hash_secret(ip or "unknown")


def safe_log_value(value: object, limit: int = _MAX_LOG_FIELD) -> str:
    """Make an attacker-controlled string safe to write to a log line.

    Device IDs and sensor names arrive from the network and land in the log
    verbatim. Without this, a newline in one of them forges a whole log entry
    and a control character can corrupt a terminal reading the file.
    """
    if value is None:
        return "-"
    text = str(value)
    cleaned = "".join(ch if ch.isprintable() and ch not in "\r\n" else "?" for ch in text)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or "-"
