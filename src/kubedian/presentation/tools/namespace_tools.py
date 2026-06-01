"""Namespace-centric MCP tools."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from kubedian.application.use_cases.queries import node_dict
from kubedian.domain.entities.graph import EdgeType, NodeType
from kubedian.presentation.tools.dependencies import get_reader, parse_env

_RO = {"readOnlyHint": True}


def register_namespace_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def list_namespaces(environment: Optional[str] = "production") -> dict:
        """List namespaces with their services and cross-namespace call counts."""
        reader = get_reader()
        env = parse_env(environment)
        by_ns: dict[str, list[dict]] = defaultdict(list)
        ns_of: dict[str, str] = {}
        for n in reader.nodes():
            if n.type == NodeType.SERVICE and n.namespace:
                by_ns[n.namespace].append(node_dict(n))
                ns_of[n.id] = n.namespace
        cross = defaultdict(int)
        for e in reader.edges(env):
            if e.type == EdgeType.HTTP_CALLS:
                s, d = ns_of.get(e.src_id), ns_of.get(e.dst_id)
                if s and d and s != d:
                    cross[f"{s} → {d}"] += 1
        return {
            "namespaces": [
                {"namespace": ns, "service_count": len(svcs), "services": svcs}
                for ns, svcs in sorted(by_ns.items())
            ],
            "cross_namespace_calls": dict(cross),
        }

    @mcp.tool(annotations=_RO)
    def namespace_context(namespace: str, environment: Optional[str] = "production") -> dict:
        """Services in a namespace plus their inbound/outbound cross-ns edges."""
        reader = get_reader()
        env = parse_env(environment)
        services = [
            node_dict(n)
            for n in reader.nodes()
            if n.type == NodeType.SERVICE and n.namespace == namespace
        ]
        return {"namespace": namespace, "service_count": len(services), "services": services}
