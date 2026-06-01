"""Shared configuration and path conventions."""

from __future__ import annotations

from pathlib import Path

DEFAULT_DB_DIRNAME = ".kubedian"
DEFAULT_DB_FILENAME = "graph.db"

SUPPORTED_LANGS = ("en", "es", "pt")
DEFAULT_LANG = "en"


def default_db_path(repo: Path) -> Path:
    return repo / DEFAULT_DB_DIRNAME / DEFAULT_DB_FILENAME
