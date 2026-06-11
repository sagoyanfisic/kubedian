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
    d = {
        "id": node.id,
        "type": node.type.value,
        "name": node.name,
        "namespace": node.namespace,
        "image": node.attrs.get("image"),
    }
    # Workload facts kept on the (collapsed) service node, when present.
    for key in ("workload_kind", "replicas", "ports", "service_account", "nodepool"):
        if node.attrs.get(key) is not None:
            d[key] = node.attrs[key]
    return d


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


def _scaler_dict(edge, reader: GraphReader) -> dict:
    """An incoming SCALES edge: relabel the source (the HPA) as `autoscaler`."""
    d = edge_dict(edge, reader, other="src_id")
    d["autoscaler"] = d.pop("service")
    return d


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
    datastores, external, calls, storage, identity, routing = [], [], [], [], [], []
    for e in callees:
        dst = reader.node(e.dst_id)
        if dst and dst.type in _DATASTORE_TYPES:
            datastores.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.CALLS_EXTERNAL:
            external.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.HTTP_CALLS:
            calls.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.MOUNTS:
            storage.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.RUNS_AS:
            identity.append(edge_dict(e, reader, other="dst_id"))
        elif e.type == EdgeType.ROUTES_TO:
            routing.append(edge_dict(e, reader, other="dst_id"))
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
        "routing": routing,
        "storage": storage,
        "identity": identity,
        # HPA -> service is an incoming SCALES edge; surface the HPA as `autoscaler`
        # rather than the generic `service` key edge_dict uses for src direction.
        "autoscaling": [_scaler_dict(e, reader) for e in callers if e.type == EdgeType.SCALES],
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


def service_secrets(reader: GraphReader, service: str, env: Environment | None) -> dict:
    """Secrets and ConfigMaps a service consumes — KEY NAMES ONLY, never values."""
    node_id = find_service_id(reader, service)
    if node_id is None:
        return {"error": f"no service matching {service!r}"}
    secrets, configmaps = [], []
    for e in reader.references(node_id, env):
        dst = reader.node(e.dst_id)
        if dst is None or dst.type not in (NodeType.SECRET, NodeType.CONFIGMAP):
            continue
        entry = {
            "name": dst.name,
            "node_id": dst.id,
            "namespace": dst.namespace,
            "modes": e.attrs.get("modes") or [e.signal.value],
            "keys": e.attrs.get("keys"),
            "env_map": e.attrs.get("env_map"),
            "provenance": e.provenance.value,
            "environment": e.environment.value,
            "source_file": e.source_file,
        }
        (secrets if dst.type == NodeType.SECRET else configmaps).append(entry)
    return {
        "service": node_dict(reader.node(node_id)),
        "secrets": secrets,
        "configmaps": configmaps,
    }


def service_ports(reader: GraphReader, service: str, env: Environment | None) -> dict:
    """Every port fact known for a service: containerPorts, the Service's
    port->targetPort wiring, and how it is exposed via Ingress/VirtualService."""
    node_id = find_service_id(reader, service)
    if node_id is None:
        return {"error": f"no service matching {service!r}"}
    node = reader.node(node_id)
    exposed = []
    for e in reader.callers(node_id, env):
        if e.type != EdgeType.ROUTES_TO:
            continue
        d = edge_dict(e, reader, other="src_id")
        d["port"] = e.attrs.get("port")
        d["port_name"] = e.attrs.get("port_name")
        exposed.append(d)
    return {
        "service": node_dict(node),
        "container_ports": node.attrs.get("ports") or [],
        "named_ports": node.attrs.get("named_ports"),
        "service_ports": node.attrs.get("port_map") or [],
        "exposed_via": exposed,
    }


def find_key_usage(
    reader: GraphReader,
    query: str,
    env: Environment | None,
    partial: bool = False,
    limit: int = 100,
) -> dict:
    """Reverse lookup by env var / secret key NAME. Returns who consumes it, from
    which Secret/ConfigMap, and through which env var — names only, never values."""
    q = query.lower()

    def _matches(name: str) -> bool:
        return q in name.lower() if partial else q == name.lower()

    matches = []
    for e in reader.find_key_refs(query, env, partial=partial):
        src = reader.node(e.src_id)
        dst = reader.node(e.dst_id)
        env_map: dict = e.attrs.get("env_map") or {}
        modes = e.attrs.get("modes") or [e.signal.value]
        base = {
            "workload": src.name if src else e.src_id,
            "workload_id": e.src_id,
            "ref": dst.name if dst else e.dst_id,
            "ref_node_id": e.dst_id,
            "ref_type": dst.type.value if dst else None,
            "environment": e.environment.value,
            "provenance": e.provenance.value,
            "source_file": e.source_file,
        }
        # Expand the edge into per-key matches: valueFrom keys carry their env var
        # name; envFrom keys ARE the var name; volume-mounted keys have no var.
        # A key consumed through several modes yields one match per mode.
        for var, key in env_map.items():
            if _matches(var) or _matches(key):
                matches.append({**base, "var": var, "key": key, "mode": "env_key_ref"})
        other_mode = next((m for m in modes if m != "env_key_ref"), None)
        if other_mode:
            for key in e.attrs.get("keys") or []:
                if not _matches(key):
                    continue
                matches.append({
                    **base,
                    "var": key if other_mode == "env_from" else None,
                    "key": key,
                    "mode": other_mode,
                })
    return {"query": query, "match_count": len(matches), "matches": matches[:limit]}


def find_port(reader: GraphReader, port: int, env: Environment | None) -> dict:
    """Who listens on / routes to a given port number."""
    listeners = [node_dict(n) for n in reader.nodes_with_port(port)]
    routes = []
    for e in reader.routes_with_port(port, env):
        d = edge_dict(e, reader, other="dst_id")
        src = reader.node(e.src_id)
        d["via"] = src.name if src else e.src_id
        d["port"] = e.attrs.get("port")
        routes.append(d)
    return {"port": port, "listeners": listeners, "routed_by": routes}


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
        # Kinds seen in manifests but not graphed — refreshed by full `index` only.
        "ignored_kinds": (meta.ignored_kinds or {}) if meta else {},
        "ignored_kind_count": sum((meta.ignored_kinds or {}).values()) if meta else 0,
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
            # Follow actual calls and declared routing (gateways route via
            # VirtualService/Ingress); ALLOWS_TO is a permission, not a call, so
            # it is deliberately excluded from request-path tracing.
            if e.type not in (EdgeType.HTTP_CALLS, EdgeType.ROUTES_TO):
                continue
            if e.dst_id not in seen:
                seen.add(e.dst_id)
                queue.append((e.dst_id, [*path, e.dst_id]))
    return None
