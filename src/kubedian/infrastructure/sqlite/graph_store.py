"""Write a resolved Graph into SQLite (the source of truth)."""

from __future__ import annotations

import json
import sqlite3
from importlib.resources import files
from pathlib import Path

from kubedian.domain.entities.graph import Graph

_SCHEMA = files("kubedian.infrastructure.sqlite").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def write_graph(
    db_path: Path,
    graph: Graph,
    *,
    repo_path: str,
    indexed_at: str,
    kustomize_version: str | None,
    render_failures: int,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO nodes(id, type, name, namespace, attrs) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (n.id, n.type.value, n.name, n.namespace, json.dumps(n.attrs))
                    for n in graph.nodes.values()
                ],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO edges"
                "(src_id, dst_id, type, environment, provenance, signal, "
                " source_file, source_locator, confidence, attrs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.src_id,
                        e.dst_id,
                        e.type.value,
                        e.environment.value,
                        e.provenance.value,
                        e.signal.value,
                        e.source_file,
                        e.source_locator,
                        e.confidence,
                        json.dumps(e.attrs),
                    )
                    for e in graph.edges
                ],
            )
            service_count = sum(1 for n in graph.nodes.values() if n.type.value == "service")
            conn.execute(
                "INSERT OR REPLACE INTO index_meta"
                "(id, repo_path, indexed_at, kustomize_version, service_count, "
                " edge_count, render_failures) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    repo_path,
                    indexed_at,
                    kustomize_version,
                    service_count,
                    len(graph.edges),
                    render_failures,
                ),
            )
    finally:
        conn.close()
