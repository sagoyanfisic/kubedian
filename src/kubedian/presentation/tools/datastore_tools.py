"""Datastore- and dependency-centric MCP tools."""

from __future__ import annotations

from typing import Optional

from kubedian.application.use_cases import queries
from kubedian.domain.entities.graph import NodeType
from kubedian.presentation.tools.dependencies import get_reader, parse_env

_RO = {"readOnlyHint": True}


def register_datastore_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def datastore_clients(datastore: str, environment: Optional[str] = "production") -> dict:
        """List every service that reads/writes/queues/caches a given datastore."""
        return queries.datastore_clients(get_reader(), datastore, parse_env(environment))

    @mcp.tool(annotations=_RO)
    def list_external_dependencies(environment: Optional[str] = "production") -> dict:
        """All external APIs the platform depends on, and who calls each."""
        reader = get_reader()
        env = parse_env(environment)
        out = []
        for node in reader.nodes():
            if node.type != NodeType.EXTERNAL_API:
                continue
            callers = reader.callers(node.id, env)
            out.append({
                **queries.node_dict(node),
                "called_by": [queries.edge_dict(e, reader, other="src_id") for e in callers],
            })
        return {"external_dependencies": out}
