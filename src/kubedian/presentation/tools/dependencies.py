"""Shared dependencies for the MCP tools.

A single GraphReader is cached for the server's lifetime, but it is **rebuilt
automatically when the graph DB changes on disk** (its path or mtime) — so a
``kubedian index`` is picked up on the next query without restarting the server.
The DB path comes from ``KUBEDIAN_DB`` (or ``KUBEDIAN_REPO``'s default
``.kubedian/graph.db``, or ./.kubedian/graph.db).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from kubedian.config import default_db_path, parse_env
from kubedian.infrastructure.sqlite.graph_reader import GraphReader

__all__ = ["get_reader", "reset_reader", "resolve_db_path", "parse_env"]

_lock = threading.Lock()
_reader: GraphReader | None = None
_reader_key: tuple[str, int] | None = None  # (db path, mtime_ns)


def resolve_db_path() -> Path:
    if db := os.getenv("KUBEDIAN_DB"):
        return Path(db)
    if repo := os.getenv("KUBEDIAN_REPO"):
        return default_db_path(Path(repo))
    return default_db_path(Path.cwd())


def _db_key(path: Path) -> tuple[str, int]:
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return (str(path), 0)


def get_reader() -> GraphReader:
    """Return a cached GraphReader, transparently reopened when the DB changes."""
    global _reader, _reader_key
    key = _db_key(resolve_db_path())
    with _lock:
        if _reader is None or _reader_key != key:
            if _reader is not None:
                try:
                    _reader.close()
                except Exception:  # pragma: no cover - close is best-effort
                    pass
            _reader = GraphReader(Path(key[0]), check_same_thread=False)
            _reader_key = key
        return _reader


def reset_reader() -> None:
    """Drop the cached reader (used by tests and on shutdown)."""
    global _reader, _reader_key
    with _lock:
        if _reader is not None:
            try:
                _reader.close()
            except Exception:  # pragma: no cover
                pass
        _reader = None
        _reader_key = None
