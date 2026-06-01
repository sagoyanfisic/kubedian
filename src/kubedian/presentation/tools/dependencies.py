"""Shared dependencies for the MCP tools.

The index is read-only and static between reindexes, so we cache a single
GraphReader for the server's lifetime. The DB path comes from ``KUBEDIAN_DB``
(or ``KUBEDIAN_REPO``'s default ``.kubedian/graph.db``, or ./.kubedian/graph.db).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from kubedian.config import default_db_path
from kubedian.domain.entities.graph import Environment
from kubedian.infrastructure.sqlite.graph_reader import GraphReader


def resolve_db_path() -> Path:
    if db := os.getenv("KUBEDIAN_DB"):
        return Path(db)
    if repo := os.getenv("KUBEDIAN_REPO"):
        return default_db_path(Path(repo))
    return default_db_path(Path.cwd())


@lru_cache(maxsize=1)
def get_reader() -> GraphReader:
    return GraphReader(resolve_db_path(), check_same_thread=False)


def parse_env(environment: str | None) -> Environment | None:
    if not environment or environment.lower() == "all":
        return None
    return Environment(environment.lower())
