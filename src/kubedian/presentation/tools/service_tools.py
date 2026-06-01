"""Service-centric MCP tools — thin wrappers over shared query use-cases."""

from __future__ import annotations

from typing import Optional

from kubedian.application.use_cases import queries
from kubedian.config import DEFAULT_LANG
from kubedian.presentation.tools.dependencies import get_reader, parse_env

_RO = {"readOnlyHint": True}


def register_service_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def service_search(query: str, limit: int = 25) -> dict:
        """Find services / datastores / external APIs by name or namespace."""
        return queries.search(get_reader(), query, limit)

    @mcp.tool(annotations=_RO)
    def service_context(service: str, environment: Optional[str] = "production") -> dict:
        """PRIMARY tool: full context for a service in ONE call — its identity,
        who calls it, what it calls, its datastores and external dependencies.
        Every edge cites provenance (explicit vs heuristic) and source file."""
        return queries.context(get_reader(), service, parse_env(environment))

    @mcp.tool(annotations=_RO)
    def service_callers(service: str, environment: Optional[str] = "production", limit: int = 100) -> dict:
        """Which services call this one (incoming http_calls / routes)."""
        return queries.callers(get_reader(), service, parse_env(environment), limit)

    @mcp.tool(annotations=_RO)
    def service_callees(service: str, environment: Optional[str] = "production", limit: int = 100) -> dict:
        """What this service depends on (services, db, cache, queue, external)."""
        return queries.callees(get_reader(), service, parse_env(environment), limit)

    @mcp.tool(annotations=_RO)
    def service_trace(source: str, target: str, environment: Optional[str] = "production", max_depth: int = 6) -> dict:
        """Find a request path between two services (transitive http_calls)."""
        return queries.trace(get_reader(), source, target, parse_env(environment), max_depth)

    @mcp.tool(annotations=_RO)
    def service_impact(service: str, environment: Optional[str] = "production", max_depth: int = 5) -> dict:
        """Blast radius: every service transitively affected if this one fails."""
        return queries.impact(get_reader(), service, parse_env(environment), max_depth)

    @mcp.tool(annotations=_RO)
    def service_diagram(
        service: Optional[str] = None,
        scope: str = "focus",
        environment: Optional[str] = "production",
        lang: str = DEFAULT_LANG,
    ) -> dict:
        """A MermaidJS diagram (string) for a service (scope='focus') or the
        whole topology (scope='all'). Paste into Obsidian, GitHub or mermaid.live."""
        from kubedian.infrastructure.mermaid import mermaid_renderer

        reader = get_reader()
        graph = reader.graph(parse_env(environment))
        if scope == "all" or not service:
            diagram = mermaid_renderer.render_flowchart(graph, lang=lang)
        else:
            node_id = queries.find_service_id(reader, service)
            if node_id is None:
                return {"error": f"no service matching {service!r}"}
            diagram = mermaid_renderer.render_focus(graph, node_id, lang=lang)
        return {"format": "mermaid", "diagram": diagram}
