"""Namespace-centric MCP tools."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from kubedian.application.use_cases import queries
from kubedian.application.use_cases.queries import node_dict
from kubedian.domain.entities.graph import EdgeType, NodeType
from kubedian.presentation.tools.dependencies import get_reader, parse_env

_RO = {"readOnlyHint": True}


def register_namespace_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def list_namespaces(environment: Optional[str] = "production") -> dict:
        """List namespaces with services, job/cronjob counts, and cross-namespace call counts."""
        reader = get_reader()
        env = parse_env(environment)
        by_ns: dict[str, list[dict]] = defaultdict(list)
        jobs_by_ns: dict[str, int] = defaultdict(int)
        cronjobs_by_ns: dict[str, int] = defaultdict(int)
        ns_of: dict[str, str] = {}
        for n in reader.nodes():
            if not n.namespace:
                continue
            if n.type == NodeType.SERVICE:
                by_ns[n.namespace].append(node_dict(n))
                ns_of[n.id] = n.namespace
            elif n.type == NodeType.JOB:
                jobs_by_ns[n.namespace] += 1
            elif n.type == NodeType.CRONJOB:
                cronjobs_by_ns[n.namespace] += 1
        cross = defaultdict(int)
        for e in reader.edges(env):
            if e.type == EdgeType.HTTP_CALLS:
                s, d = ns_of.get(e.src_id), ns_of.get(e.dst_id)
                if s and d and s != d:
                    cross[f"{s} → {d}"] += 1
        all_ns = sorted(set(by_ns) | set(jobs_by_ns) | set(cronjobs_by_ns))
        return {
            "namespaces": [
                {
                    "namespace": ns,
                    "service_count": len(by_ns.get(ns, [])),
                    "services": by_ns.get(ns, []),
                    "job_count": jobs_by_ns.get(ns, 0),
                    "cronjob_count": cronjobs_by_ns.get(ns, 0),
                }
                for ns in all_ns
            ],
            "cross_namespace_calls": dict(cross),
        }

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
        reader = get_reader()
        services: list[dict] = []
        jobs: list[dict] = []
        cronjobs: list[dict] = []
        for n in reader.nodes():
            if n.namespace != namespace:
                continue
            if n.type == NodeType.SERVICE:
                services.append(node_dict(n))
            elif n.type == NodeType.JOB:
                jobs.append(node_dict(n))
            elif n.type == NodeType.CRONJOB:
                cronjobs.append(node_dict(n))
        return {
            "namespace": namespace,
            "service_count": len(services),
            "services": services,
            "job_count": len(jobs),
            "jobs": jobs,
            "cronjob_count": len(cronjobs),
            "cronjobs": cronjobs,
        }
