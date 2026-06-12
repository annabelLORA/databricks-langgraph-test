"""File store with Databricks Volumes backend and in-memory fallback.

Set VOLUME_PATH env var to a Databricks Volumes mount (e.g. /Volumes/main/hse/outputs)
to persist generated files across workers and pod restarts. Without it, falls back to
an in-memory store with a 1-hour TTL (fine for local dev, breaks on multi-worker deploys).
"""

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600  # 1 hour

# ── In-memory fallback ────────────────────────────────────────────────────────

# token -> (content_bytes, filename, expiry_timestamp)
_store: dict[str, tuple[bytes, str, float]] = {}


def _evict_expired() -> None:
    now = time.time()
    expired = [t for t, (_, _, exp) in _store.items() if now > exp]
    for t in expired:
        del _store[t]


# ── Volume backend ────────────────────────────────────────────────────────────

def _volume_dir() -> Optional[Path]:
    path = os.environ.get("VOLUME_PATH", "").strip()
    if not path:
        return None
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception as e:
        logger.warning("VOLUME_PATH %s not usable, falling back to in-memory store: %s", path, e)
        return None


def _volume_meta_path(vol: Path, token: str) -> Path:
    return vol / f"{token}.meta"


def _volume_data_path(vol: Path, token: str) -> Path:
    return vol / f"{token}.xlsx"


# ── Public API ────────────────────────────────────────────────────────────────

def store_file(content: bytes, filename: str) -> str:
    token = secrets.token_urlsafe(16)
    vol = _volume_dir()
    if vol is not None:
        try:
            _volume_data_path(vol, token).write_bytes(content)
            _volume_meta_path(vol, token).write_text(
                f"{filename}\n{time.time() + _TTL_SECONDS}", encoding="utf-8"
            )
            logger.debug("Stored %s in Volume at %s", filename, vol)
            return token
        except Exception as e:
            logger.warning("Volume write failed, falling back to in-memory: %s", e)

    _evict_expired()
    _store[token] = (content, filename, time.time() + _TTL_SECONDS)
    return token


def get_file(token: str) -> Optional[tuple[bytes, str]]:
    vol = _volume_dir()
    if vol is not None:
        meta_path = _volume_meta_path(vol, token)
        data_path = _volume_data_path(vol, token)
        if meta_path.exists() and data_path.exists():
            try:
                lines = meta_path.read_text(encoding="utf-8").splitlines()
                filename, expiry = lines[0], float(lines[1])
                if time.time() > expiry:
                    meta_path.unlink(missing_ok=True)
                    data_path.unlink(missing_ok=True)
                    return None
                return data_path.read_bytes(), filename
            except Exception as e:
                logger.warning("Volume read failed for token %s: %s", token, e)

    entry = _store.get(token)
    if entry is None:
        return None
    content, filename, expiry = entry
    if time.time() > expiry:
        del _store[token]
        return None
    return content, filename
