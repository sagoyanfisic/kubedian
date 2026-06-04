"""Smoke test the MCP server over an in-memory client (skips without fastmcp)."""

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402

from kubedian.application.pipeline.index import index_repo  # noqa: E402
from kubedian.domain.entities.graph import Environment  # noqa: E402
from tests.conftest import write_sample_repo  # noqa: E402


@pytest.fixture()
def indexed_db(tmp_path, monkeypatch):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    monkeypatch.setenv("KUBEDIAN_DB", str(db))
    # reset the cached reader so it picks up this DB
    from kubedian.presentation.tools.dependencies import reset_reader

    reset_reader()
    return db


def test_reader_reloads_when_db_mtime_changes(tmp_path, monkeypatch):
    """The MCP caches the reader, but a reindex (which bumps the DB's mtime) must be
    picked up on the next query without restarting the server."""
    import os
    import time

    from kubedian.presentation.tools.dependencies import get_reader, reset_reader

    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    monkeypatch.setenv("KUBEDIAN_DB", str(db))
    reset_reader()

    first = get_reader()
    assert get_reader() is first  # unchanged DB → same cached reader

    # simulate a reindex touching the file (advance mtime)
    future = time.time() + 5
    os.utime(db, (future, future))

    assert get_reader() is not first  # mtime changed → reader transparently reopened
    reset_reader()


async def test_mcp_tools_registered_and_context(indexed_db):
    from kubedian.main import mcp

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"service_context", "service_impact", "service_trace", "index_status"} <= names

        res = await client.call_tool(
            "service_context", {"service": "service-a", "environment": "staging"}
        )
        data = res.data
        assert data["service"]["name"] == "service-a"
        assert any(c["target"] == "service-b" for c in data["calls"])
        # heuristic datastore edges present and tagged
        assert any(d["provenance"] == "heuristic" for d in data["datastores"])


async def test_mcp_diagram_tool(indexed_db):
    from kubedian.main import mcp

    async with Client(mcp) as client:
        res = await client.call_tool("service_diagram", {"scope": "all", "lang": "es"})
        assert res.data["format"] == "mermaid"
        assert "flowchart" in res.data["diagram"]
