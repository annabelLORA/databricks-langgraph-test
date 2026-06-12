"""In-memory file store for ephemeral download tokens."""

import secrets
import time
from typing import Optional

# token -> (content_bytes, filename, expiry_timestamp)
_store: dict[str, tuple[bytes, str, float]] = {}

_TTL_SECONDS = 3600  # 1 hour


def store_file(content: bytes, filename: str) -> str:
    _evict_expired()
    token = secrets.token_urlsafe(16)
    _store[token] = (content, filename, time.time() + _TTL_SECONDS)
    return token


def get_file(token: str) -> Optional[tuple[bytes, str]]:
    entry = _store.get(token)
    if entry is None:
        return None
    content, filename, expiry = entry
    if time.time() > expiry:
        del _store[token]
        return None
    return content, filename


def _evict_expired() -> None:
    now = time.time()
    expired = [t for t, (_, _, exp) in _store.items() if now > exp]
    for t in expired:
        del _store[t]
