"""Config-centric MCP tools: secrets/configmaps, ports and reverse key lookup.

Every answer here is built from key/var NAMES only — secret values are never
stored in the graph, so they can never be returned.
"""

from __future__ import annotations

from typing import Optional

from kubedian.application.use_cases import queries
from kubedian.presentation.tools.dependencies import get_reader, parse_env

_RO = {"readOnlyHint": True}


def register_config_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def service_secrets(service: str, environment: Optional[str] = "production") -> dict:
        """Secrets and ConfigMaps a service consumes: KEY NAMES ONLY (never
        values), with consumption mode (env_from / env_key_ref / volume_mount)
        and, for valueFrom wiring, the env var -> key mapping."""
        return queries.service_secrets(get_reader(), service, parse_env(environment))

    @mcp.tool(annotations=_RO)
    def service_ports(service: str, environment: Optional[str] = "production") -> dict:
        """Every port fact for a service: containerPorts, the k8s Service's
        port->targetPort wiring, and how it is exposed via Ingress/VirtualService
        (with the routed port when declared)."""
        return queries.service_ports(get_reader(), service, parse_env(environment))

    @mcp.tool(annotations=_RO)
    def find_key_usage(query: str, environment: Optional[str] = "all", partial: bool = False) -> dict:
        """Reverse lookup by env var or secret/configmap KEY NAME (e.g.
        'POSTGRES_HOST'): which workloads consume it, from which Secret/ConfigMap,
        through which env var, in which environment. Names only — never values.
        Set partial=true for substring matching."""
        return queries.find_key_usage(get_reader(), query, parse_env(environment), partial=partial)

    @mcp.tool(annotations=_RO)
    def find_port(port: int, environment: Optional[str] = "all") -> dict:
        """Reverse lookup by port number: which services listen on it
        (containerPort / Service port / targetPort) and which Ingress or
        VirtualService routes route to it."""
        return queries.find_port(get_reader(), port, parse_env(environment))
