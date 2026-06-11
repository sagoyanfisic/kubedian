"""Read the service graph back from SQLite.

Shared by every read surface (vault/mermaid/docs exporters and the MCP server),
so graph traversal logic lives in exactly one place. The DB is opened read-only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kubedian.domain.entities.graph import (
    Edge,
    EdgeType,
    Environment,
    Graph,
    Node,
    NodeType,
    Provenance,
    Signal,
)


@dataclass
class IndexMeta:
    repo_path: str | None
    indexed_at: str | None
    kustomize_version: str | None
    service_count: int
    edge_count: int
    render_failures: int
    # kind -> count of rendered resources the resolver does not graph.
    # Only refreshed by a full `index` (sync-envs doesn't own it).
    ignored_kinds: dict[str, int] | None = None


class GraphReader:
    def __init__(self, db_path: Path, check_same_thread: bool = True):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"no index at {self.db_path}. Run `kubedian index --repo <repo>` first."
            )
        uri = f"file:{self.db_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
        self._conn.row_factory = sqlite3.Row

    # -- nodes ----------------------------------------------------------------
    def _node(self, row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"],
            type=NodeType(row["type"]),
            name=row["name"],
            namespace=row["namespace"],
            attrs=json.loads(row["attrs"] or "{}"),
        )

    def node(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._node(row) if row else None

    def nodes(self) -> list[Node]:
        return [self._node(r) for r in self._conn.execute("SELECT * FROM nodes")]

    def search(self, query: str, limit: int = 25, namespace: str | None = None) -> list[Node]:
        like = f"%{query.lower()}%"
        ns_clause, ns_params = (" AND namespace = ?", [namespace]) if namespace else ("", [])
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE (lower(name) LIKE ? OR lower(id) LIKE ?){ns_clause} "
            "ORDER BY (type='service') DESC, name LIMIT ?",
            (like, like, *ns_params, limit),
        )
        return [self._node(r) for r in rows]

    def nodes_in_namespace(self, namespace: str) -> list[Node]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE namespace = ? ORDER BY type, name", (namespace,)
        )
        return [self._node(r) for r in rows]

    def bundle_siblings(self, overlay: str, namespace: str, exclude_id: str) -> list[Node]:
        """Other workloads rendered from the same overlay directory (an
        api/worker/beat bundle) — identified by the `overlay` attr stamped on
        every workload node."""
        rows = self._conn.execute(
            """
            SELECT * FROM nodes
            WHERE json_extract(attrs, '$.overlay') = ? AND namespace = ?
              AND id != ? AND type IN ('service', 'job', 'cronjob')
            ORDER BY name
            """,
            (overlay, namespace, exclude_id),
        )
        return [self._node(r) for r in rows]

    # -- edges ----------------------------------------------------------------
    def _edge(self, row: sqlite3.Row) -> Edge:
        return Edge(
            src_id=row["src_id"],
            dst_id=row["dst_id"],
            type=EdgeType(row["type"]),
            environment=Environment(row["environment"]),
            provenance=Provenance(row["provenance"]),
            signal=Signal(row["signal"]),
            source_file=row["source_file"],
            source_locator=row["source_locator"],
            confidence=row["confidence"],
            attrs=json.loads(row["attrs"] or "{}"),
        )

    def _env_clause(self, environment: Environment | None) -> tuple[str, list]:
        return (" AND environment = ?", [environment.value]) if environment else ("", [])

    def callees(self, node_id: str, environment: Environment | None = None) -> list[Edge]:
        clause, params = self._env_clause(environment)
        rows = self._conn.execute(
            f"SELECT * FROM edges WHERE src_id = ?{clause} ORDER BY type", [node_id, *params]
        )
        return [self._edge(r) for r in rows]

    def callers(self, node_id: str, environment: Environment | None = None) -> list[Edge]:
        clause, params = self._env_clause(environment)
        rows = self._conn.execute(
            f"SELECT * FROM edges WHERE dst_id = ?{clause} ORDER BY type", [node_id, *params]
        )
        return [self._edge(r) for r in rows]

    def edges(self, environment: Environment | None = None) -> list[Edge]:
        clause, params = self._env_clause(environment)
        where = (" WHERE 1=1" + clause) if clause else ""
        rows = self._conn.execute(f"SELECT * FROM edges{where}", params)
        return [self._edge(r) for r in rows]

    def references(self, node_id: str, environment: Environment | None = None) -> list[Edge]:
        """Outgoing REFERENCES edges of a workload (its Secrets/ConfigMaps)."""
        clause, params = self._env_clause(environment)
        rows = self._conn.execute(
            f"SELECT * FROM edges WHERE src_id = ? AND type = ?{clause} ORDER BY dst_id",
            [node_id, EdgeType.REFERENCES.value, *params],
        )
        return [self._edge(r) for r in rows]

    def find_key_refs(
        self,
        query: str,
        environment: Environment | None = None,
        *,
        partial: bool = False,
    ) -> list[Edge]:
        """Reverse lookup: REFERENCES edges whose consumed key names — or the env
        var names mapped to them — match ``query``. Matches NAMES only; secret
        values are never stored, so they can never be searched or returned."""
        clause, params = self._env_clause(environment)
        if partial:
            match, arg = "LIKE ? COLLATE NOCASE", f"%{query}%"
        else:
            match, arg = "= ? COLLATE NOCASE", query
        sql = f"""
            SELECT DISTINCT e.* FROM edges e
            WHERE e.type = ?{clause} AND (
                EXISTS (SELECT 1 FROM json_each(e.attrs, '$.keys') k
                        WHERE k.value {match})
                OR EXISTS (SELECT 1 FROM json_each(e.attrs, '$.env_map') m
                           WHERE m.key {match} OR m.value {match})
            )
            ORDER BY e.src_id
        """
        rows = self._conn.execute(
            sql, [EdgeType.REFERENCES.value, *params, arg, arg, arg]
        )
        return [self._edge(r) for r in rows]

    def nodes_with_port(self, port: int) -> list[Node]:
        """Nodes that listen on / expose ``port`` (containerPorts or Service ports)."""
        sql = """
            SELECT DISTINCT n.* FROM nodes n
            WHERE EXISTS (SELECT 1 FROM json_each(n.attrs, '$.ports') p
                          WHERE p.value = ?)
               OR EXISTS (SELECT 1 FROM json_each(n.attrs, '$.port_map') pm
                          WHERE json_extract(pm.value, '$.port') = ?
                             OR json_extract(pm.value, '$.target_port') = ?)
            ORDER BY n.name
        """
        rows = self._conn.execute(sql, [port, port, port])
        return [self._node(r) for r in rows]

    def routes_with_port(self, port: int, environment: Environment | None = None) -> list[Edge]:
        """ROUTES_TO edges (Ingress/VirtualService) that target ``port``."""
        clause, params = self._env_clause(environment)
        sql = f"""
            SELECT * FROM edges
            WHERE type = ?{clause} AND json_extract(attrs, '$.port') = ?
            ORDER BY src_id
        """
        rows = self._conn.execute(sql, [EdgeType.ROUTES_TO.value, *params, port])
        return [self._edge(r) for r in rows]

    def graph(self, environment: Environment | None = None) -> Graph:
        """Load the full graph, optionally filtered to one environment.

        Only nodes touched by the kept edges (plus all service nodes) are kept.
        """
        edges = self.edges(environment)
        graph = Graph()
        keep: set[str] = set()
        for e in edges:
            keep.add(e.src_id)
            keep.add(e.dst_id)
        for n in self.nodes():
            if n.type == NodeType.SERVICE or n.id in keep:
                graph.add_node(n)
        graph.edges = edges
        return graph

    def impact(self, node_id: str, environment: Environment | None = None, max_depth: int = 5) -> list[str]:
        """Transitive callers (blast radius) up to ``max_depth`` hops."""
        clause, params = self._env_clause(environment)
        sql = f"""
            WITH RECURSIVE blast(id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT e.src_id, b.depth + 1
                FROM edges e JOIN blast b ON e.dst_id = b.id
                WHERE b.depth < ?{clause}
            )
            SELECT DISTINCT id FROM blast WHERE id != ?
        """
        rows = self._conn.execute(sql, [node_id, max_depth, *params, node_id])
        return [r["id"] for r in rows]

    def meta(self) -> IndexMeta | None:
        row = self._conn.execute("SELECT * FROM index_meta WHERE id = 1").fetchone()
        if not row:
            return None
        # Defensive: pre-migration DBs may lack the column.
        ignored = row["ignored_kinds"] if "ignored_kinds" in row.keys() else None
        return IndexMeta(
            repo_path=row["repo_path"],
            indexed_at=row["indexed_at"],
            kustomize_version=row["kustomize_version"],
            service_count=row["service_count"],
            edge_count=row["edge_count"],
            render_failures=row["render_failures"],
            ignored_kinds=json.loads(ignored) if ignored else None,
        )

    def close(self) -> None:
        self._conn.close()
