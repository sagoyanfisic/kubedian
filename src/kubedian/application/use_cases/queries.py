"""Read-only query use-cases shared by the CLI and the MCP server.

Both surfaces call these so behaviour is identical. Each function takes a
GraphReader and returns plain dicts (already serialised, provenance included).
"""

from __future__ import annotations

from collections import deque

from kubedian.domain.entities.graph import EdgeType, Environment, NodeType
from kubedian.infrastructure.sqlite.graph_reader import GraphReader

_DATASTORE_TYPES = {NodeType.DATABASE, NodeType.CACHE, NodeType.QUEUE}


# --------------------------------------------------------------------------- #
# serialisation helpers
# --------------------------------------------------------------------------- #
def node_dict(node) -> dict:
    return {
        "id": node.id,
        "type": node.type.value,
        "name": node.name,
        "namespace": node.namespace,
        "image": node.attrs.get("image"),
    }


def edge_dict(edge, reader: GraphReader, *, other: str) -> dict:
    target = reader.node(getattr(edge, other))
    label = "service" if other == "src_id" else "target"
    return {
        label: target.name if target else getattr(edge, other),
        "node_id": getattr(edge, other),
        "type": edge.type.value,
        "provenance": edge.provenance.value,
        "confidence": edge.confidence,
        "environment": edge.environment.value,
        "source_file": edge.source_file,
        "source_locator": edge.source_locator,
        "locators": edge.attrs.get("locators"),
    }


def find_service_id(reader: GraphReader, ref: str) -> str | None:
    if reader.node(ref):
        return ref
    matches = reader.search(ref, limit=50)
    services = [n for n in matches if n.type == NodeType.SERVICE]
    pool = services or matches
    return pool[0].id if pool else None


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def search(reader: GraphReader, query: str, limit: int = 25) -> dict:
    return {"results": [node_dict(n) for n in reader.search(query, limit)]}


def context(reader: GraphReader, service: str, env: Environment | None) -> dict:
    node_id = find_service_id(reader, service)
    if node_id is None:
        return {"error": f"no service matching {service!r}"}
    node = reader.node(node_id)
    callees = reader.callees(node_id, env)
    callers = reader.callers(node_id, env)
    datastores, external, calls = [], [], []
    for e in callees:
        dst = reader.node(e.dst_id)
        if dst and dst.type in _DATASTORE_TYPES:
            datastores.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.CALLS_EXTERNAL:
            external.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.HTTP_CALLS:
            calls.append(edge_dict(e, reader, other="dst_id"))
    return {
        "service": node_dict(node),
        "calls": calls,
        "called_by": [
            edge_dict(e, reader, other="src_id")
            for e in callers
            if e.type == EdgeType.HTTP_CALLS
        ],
        "datastores": datastores,
        "external_dependencies": external,
    }


def callers(reader: GraphReader, service: str, env: Environment | None, limit: int = 100) -> dict:
    node_id = find_service_id(reader, service)
    if node_id is None:
        return {"error": f"no service matching {service!r}"}
    edges = [
        e
        for e in reader.callers(node_id, env)
        if e.type in (EdgeType.HTTP_CALLS, EdgeType.ROUTES_TO)
    ][:limit]
    return {"service": node_id, "callers": [edge_dict(e, reader, other="src_id") for e in edges]}


def callees(reader: GraphReader, service: str, env: Environment | None, limit: int = 100) -> dict:
    node_id = find_service_id(reader, service)
    if node_id is None:
        return {"error": f"no service matching {service!r}"}
    edges = reader.callees(node_id, env)[:limit]
    return {"service": node_id, "callees": [edge_dict(e, reader, other="dst_id") for e in edges]}


def trace(reader: GraphReader, source: str, target: str, env: Environment | None, max_depth: int = 6) -> dict:
    src = find_service_id(reader, source)
    dst = find_service_id(reader, target)
    if src is None or dst is None:
        return {"error": "source or target not found"}
    path = _bfs_path(reader, src, dst, env, max_depth)
    return {"source": src, "target": dst, "path": path, "reachable": path is not None}


def impact(reader: GraphReader, service: str, env: Environment | None, max_depth: int = 5) -> dict:
    node_id = find_service_id(reader, service)
    if node_id is None:
        return {"error": f"no service matching {service!r}"}
    ids = reader.impact(node_id, env, max_depth)
    affected = [
        node_dict(n) for nid in ids if (n := reader.node(nid)) and n.type == NodeType.SERVICE
    ]
    return {"service": node_id, "affected_count": len(affected), "affected": affected}


def datastore_clients(reader: GraphReader, datastore: str, env: Environment | None) -> dict:
    node_id = find_service_id(reader, datastore)
    if node_id is None:
        return {"error": f"no datastore matching {datastore!r}"}
    edges = reader.callers(node_id, env)
    return {"datastore": node_id, "clients": [edge_dict(e, reader, other="src_id") for e in edges]}


def status(reader: GraphReader) -> dict:
    from collections import Counter

    meta = reader.meta()
    node_types = Counter(n.type.value for n in reader.nodes())
    edge_types = Counter(e.type.value for e in reader.edges())
    return {
        "db_path": str(reader.db_path),
        "indexed_at": meta.indexed_at if meta else None,
        "repo_path": meta.repo_path if meta else None,
        "kustomize_version": meta.kustomize_version if meta else None,
        "service_count": meta.service_count if meta else node_types.get("service", 0),
        "edge_count": meta.edge_count if meta else len(reader.edges()),
        "render_failures": meta.render_failures if meta else None,
        "node_types": dict(node_types),
        "edge_types": dict(edge_types),
    }


def _bfs_path(reader, src, dst, env, max_depth):
    queue = deque([(src, [src])])
    seen = {src}
    while queue:
        node, path = queue.popleft()
        if node == dst:
            return path
        if len(path) > max_depth:
            continue
        for e in reader.callees(node, env):
            if e.type != EdgeType.HTTP_CALLS:
                continue
            if e.dst_id not in seen:
                seen.add(e.dst_id)
                queue.append((e.dst_id, [*path, e.dst_id]))
    return None
