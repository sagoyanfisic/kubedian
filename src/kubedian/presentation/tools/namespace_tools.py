"""Namespace-centric MCP tools — thin wrappers over shared query use-cases."""

from __future__ import annotations

from typing import Optional

from kubedian.application.use_cases import queries
from kubedian.presentation.tools.dependencies import get_reader, parse_env

_RO = {"readOnlyHint": True}


def register_namespace_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def list_namespaces(environment: Optional[str] = "production") -> dict:
        """List namespaces with services, job/cronjob counts, and cross-namespace call counts."""
        return queries.list_namespaces(get_reader(), parse_env(environment))

    @mcp.tool(annotations=_RO)
    def namespace_contents(namespace: str, environment: Optional[str] = "production") -> dict:
        """Everything that lives in a namespace, grouped by node type (services,
        jobs, secrets/configmaps, PVCs, HPAs, service accounts, roles…), plus an
        aggregate of its cross-namespace relations: which namespaces it calls /
        is called from, by edge type, with example edges."""
        return queries.namespace_contents(get_reader(), namespace, parse_env(environment))

    @mcp.tool(annotations=_RO)
    def namespace_context(namespace: str, environment: Optional[str] = "production") -> dict:
        """All workloads in a namespace: Deployments/StatefulSets (services), Jobs and CronJobs."""
        return queries.namespace_context(get_reader(), namespace)
