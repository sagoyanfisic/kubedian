"""Write a resolved Graph into SQLite (the source of truth)."""

from __future__ import annotations

import json
import sqlite3
from importlib.resources import files
from pathlib import Path

from kubedian.domain.entities.graph import Graph

_SCHEMA = files("kubedian.infrastructure.sqlite").joinpath("schema.sql").read_text(encoding="utf-8")


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add the ``generation`` columns to DBs created before they existed."""
    for table in ("nodes", "edges", "index_meta"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "generation" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def write_graph(
    db_path: Path,
    graph: Graph,
    *,
    repo_path: str,
    indexed_at: str,
    kustomize_version: str | None,
    render_failures: int,
    generation: int = 1,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO nodes(id, type, name, namespace, attrs, generation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (n.id, n.type.value, n.name, n.namespace, json.dumps(n.attrs), generation)
                    for n in graph.nodes.values()
                ],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO edges"
                "(src_id, dst_id, type, environment, provenance, signal, "
                " source_file, source_locator, confidence, attrs, generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        generation,
                    )
                    for e in graph.edges
                ],
            )
            service_count = sum(1 for n in graph.nodes.values() if n.type.value == "service")
            conn.execute(
                "INSERT OR REPLACE INTO index_meta"
                "(id, repo_path, indexed_at, kustomize_version, service_count, "
                " edge_count, render_failures, generation) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_path,
                    indexed_at,
                    kustomize_version,
                    service_count,
                    len(graph.edges),
                    render_failures,
                    generation,
                ),
            )
    finally:
        conn.close()


def current_generation(db_path: Path) -> int:
    """Highest mark-and-sweep generation written so far (0 if never indexed)."""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT generation FROM index_meta WHERE id = 1").fetchone()
        return int(row["generation"]) if row and row["generation"] is not None else 0
    finally:
        conn.close()


def sync_env_subset(
    db_path: Path,
    nodes: list,
    edges: list,
    *,
    sweep_node_types: tuple[str, ...],
    sweep_edge_signals: tuple[str, ...],
    indexed_at: str,
) -> dict:
    """Mark-and-sweep update of the secret/env subset only.

    Writes ``nodes``/``edges`` at a fresh generation, then deletes the rows in the
    swept scope (``sweep_node_types`` / ``sweep_edge_signals``) left at an older
    generation — i.e. relations that no longer exist in the manifests. Atomic: the
    whole thing runs in one transaction so readers never see a half-synced graph.
    Never writes secret values — callers pass only key names + file locations.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"no index at {db_path} — run `kubedian index` first")
    conn = connect(db_path)
    try:
        with conn:
            gen = current_generation_conn(conn) + 1
            conn.executemany(
                "INSERT OR REPLACE INTO nodes(id, type, name, namespace, attrs, generation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(n.id, n.type.value, n.name, n.namespace, json.dumps(n.attrs), gen) for n in nodes],
            )
            # source_locator participates in the UNIQUE key, so REPLACE refreshes the
            # right row and bumps its generation in place.
            conn.executemany(
                "INSERT OR REPLACE INTO edges"
                "(src_id, dst_id, type, environment, provenance, signal, "
                " source_file, source_locator, confidence, attrs, generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.src_id, e.dst_id, e.type.value, e.environment.value,
                        e.provenance.value, e.signal.value, e.source_file,
                        e.source_locator, e.confidence, json.dumps(e.attrs), gen,
                    )
                    for e in edges
                ],
            )
            node_ph = ",".join("?" * len(sweep_node_types))
            edge_ph = ",".join("?" * len(sweep_edge_signals))
            swept_edges = conn.execute(
                f"DELETE FROM edges WHERE generation < ? AND signal IN ({edge_ph})",
                (gen, *sweep_edge_signals),
            ).rowcount
            swept_nodes = conn.execute(
                f"DELETE FROM nodes WHERE generation < ? AND type IN ({node_ph})",
                (gen, *sweep_node_types),
            ).rowcount
            conn.execute(
                "UPDATE index_meta SET generation = ?, indexed_at = ? WHERE id = 1",
                (gen, indexed_at),
            )
        return {
            "generation": gen,
            "nodes_written": len(nodes),
            "edges_written": len(edges),
            "nodes_swept": swept_nodes,
            "edges_swept": swept_edges,
        }
    finally:
        conn.close()


def current_generation_conn(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT generation FROM index_meta WHERE id = 1").fetchone()
    return int(row["generation"]) if row and row["generation"] is not None else 0
