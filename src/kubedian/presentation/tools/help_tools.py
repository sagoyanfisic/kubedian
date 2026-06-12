"""Meta/help MCP tool: a self-describing catalog of every registered tool.

Lets an agent answer "what can Kubedian do?" explicitly — the catalog is built by
introspecting the live tool registry (``mcp.list_tools()``), so it can never drift
from what the server actually exposes.
"""

from __future__ import annotations

from kubedian import __version__
from kubedian.application.use_cases import queries
from kubedian.presentation.tools.dependencies import ToolError, get_reader, resolve_db_path

_RO = {"readOnlyHint": True}

# Buckets only — completeness comes from introspection, unknown names fall into "other".
_CATEGORY = {
    "service_search": "service",
    "service_context": "service",
    "service_composition": "service",
    "service_callers": "service",
    "service_callees": "service",
    "service_trace": "service",
    "service_impact": "service",
    "service_diagram": "service",
    "service_secrets": "config",
    "service_ports": "config",
    "find_key_usage": "config",
    "find_port": "config",
    "datastore_clients": "datastore",
    "list_external_dependencies": "datastore",
    "list_namespaces": "namespace",
    "namespace_contents": "namespace",
    "namespace_context": "namespace",
    "index_status": "status",
    "kubedian_capabilities": "help",
}


def register_help_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    async def kubedian_capabilities() -> dict:
        """Catalog of every Kubedian tool (grouped by category) plus server identity,
        the active graph DB and a summary of its index. Call this to answer
        'what tools does Kubedian have?' or to pick the right tool for a question."""
        categories: dict[str, list[dict]] = {}
        for tool in await mcp.list_tools():
            summary = (tool.description or "").strip().splitlines()[0] if tool.description else ""
            entry = {"name": tool.name, "summary": summary}
            categories.setdefault(_CATEGORY.get(tool.name, "other"), []).append(entry)

        try:
            full = queries.status(get_reader())
            index = {k: full.get(k) for k in ("indexed_at", "service_count", "edge_count", "render_failures")}
        except (ToolError, FileNotFoundError) as exc:
            index = {"error": str(exc)}

        return {
            "server": {"name": "Kubedian", "version": __version__},
            "db_path": str(resolve_db_path()),
            "suggested_entry_points": ["service_context", "service_search", "index_status"],
            "tools": categories,
            "index": index,
        }
