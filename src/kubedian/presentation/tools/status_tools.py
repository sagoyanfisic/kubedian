"""Index status MCP tool."""

from __future__ import annotations

from kubedian.application.use_cases import queries
from kubedian.presentation.tools.dependencies import get_reader

_RO = {"readOnlyHint": True}


def register_status_tools(mcp) -> None:
    @mcp.tool(annotations=_RO)
    def index_status() -> dict:
        """Index health: when it was built, counts, and render fallbacks."""
        return queries.status(get_reader())
