"""Kubedian MCP server — query the service topology like codegraph queries code.

Run over stdio (default, for Claude Code) or HTTP. The DB is selected via the
``KUBEDIAN_DB`` env var (or ``KUBEDIAN_REPO``, or ./.kubedian/graph.db).
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from kubedian.presentation.tools.config_tools import register_config_tools
from kubedian.presentation.tools.datastore_tools import register_datastore_tools
from kubedian.presentation.tools.namespace_tools import register_namespace_tools
from kubedian.presentation.tools.service_tools import register_service_tools
from kubedian.presentation.tools.status_tools import register_status_tools

INSTRUCTIONS = (
    "Kubedian exposes the dependency graph BETWEEN microservices, extracted from "
    "Kubernetes/Kustomize manifests. Use it for service-level questions: who calls a "
    "service, what it depends on, request paths, blast radius, datastore clients, and "
    "per-namespace topology. This is complementary to codegraph: codegraph answers "
    "questions WITHIN a repo (symbols, call graphs); Kubedian answers questions BETWEEN "
    "services (HTTP calls, databases, queues, external APIs). Start with `service_context` "
    "for an overview of one service. Every edge carries provenance: 'explicit' (the "
    "ConfigMap/manifest states it) or 'heuristic' (inferred from a secret key name such as "
    "POSTGRES_HOST) with a confidence score — never present a heuristic edge as a hard fact; "
    "cite the source_file/source_locator. Config questions are first-class too: "
    "`service_secrets` (which Secrets/ConfigMaps a service consumes — key NAMES only, "
    "values are never stored), `service_ports` (containerPorts, Service port->targetPort, "
    "Ingress/VirtualService exposure), and reverse lookups `find_key_usage` (who uses env "
    "var/key X) and `find_port` (who listens on port N). For the full technical "
    "composition of one service (containers with roles/resources/probes, bundle siblings, "
    "config, storage, RBAC, NetworkPolicy connectivity, exposure) use `service_composition`; "
    "for everything living in a namespace plus its cross-namespace relations use "
    "`namespace_contents`. Most tools take an `environment` "
    "(development|staging|production|test, default production)."
)

mcp = FastMCP(name="Kubedian", instructions=INSTRUCTIONS)

register_service_tools(mcp)
register_config_tools(mcp)
register_datastore_tools(mcp)
register_namespace_tools(mcp)
register_status_tools(mcp)


def main() -> None:
    transport = os.getenv("KUBEDIAN_MCP_TRANSPORT", "stdio")
    if transport == "http":
        mcp.run(
            transport="http",
            host=os.getenv("UVICORN_HOST", "0.0.0.0"),
            port=int(os.getenv("UVICORN_PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
