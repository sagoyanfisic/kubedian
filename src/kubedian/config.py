"""Shared configuration and path conventions."""

from __future__ import annotations

from pathlib import Path

from kubedian.domain.entities.graph import Environment

DEFAULT_DB_DIRNAME = ".kubedian"
DEFAULT_DB_FILENAME = "graph.db"

SUPPORTED_LANGS = ("en", "es", "pt")
DEFAULT_LANG = "en"


def default_db_path(repo: Path) -> Path:
    return repo / DEFAULT_DB_DIRNAME / DEFAULT_DB_FILENAME


def parse_env(environment: str | None) -> Environment | None:
    """Canonical environment parsing for every surface (CLI and MCP).

    ``None`` and ``"all"`` mean no filter. Raises ValueError on unknown values —
    callers translate it to their own error style (typer.BadParameter, MCP error).
    """
    if not environment or environment.lower() == "all":
        return None
    return Environment(environment.lower())
