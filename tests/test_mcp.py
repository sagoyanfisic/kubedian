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


async def test_mcp_config_tools(indexed_db):
    from kubedian.main import mcp

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"service_secrets", "service_ports", "find_key_usage", "find_port"} <= names

        res = await client.call_tool(
            "service_secrets", {"service": "service-a", "environment": "staging"}
        )
        secrets = {s["name"]: s for s in res.data["secrets"]}
        assert "POSTGRES_HOST" in secrets["service-a-secret"]["keys"]
        assert "ENC[" not in str(res.data)  # never values, only key names

        res = await client.call_tool(
            "service_ports", {"service": "service-b", "environment": "staging"}
        )
        assert res.data["container_ports"] == [8080]
        assert any(p["port"] == 80 and p["target_port"] == 8080 for p in res.data["service_ports"])

        res = await client.call_tool("find_key_usage", {"query": "POSTGRES_HOST"})
        assert any(
            m["workload"] == "service-a" and m["ref"] == "service-a-secret"
            for m in res.data["matches"]
        )

        res = await client.call_tool("find_port", {"port": 8080})
        assert any(n["name"] == "service-b" for n in res.data["listeners"])


async def test_capabilities_tool(indexed_db):
    from kubedian import __version__
    from kubedian.main import mcp

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert "kubedian_capabilities" in names
        assert len(names) >= 19

        res = await client.call_tool("kubedian_capabilities", {})
        data = res.data
        assert data["server"]["name"] == "Kubedian"
        assert data["server"]["version"] == __version__
        listed = {t["name"] for group in data["tools"].values() for t in group}
        assert listed == names  # the catalog mirrors the live registry
        assert "other" not in data["tools"]  # every tool has a category
        assert "service_context" in data["suggested_entry_points"]
        assert data["index"]["service_count"] > 0


async def test_server_version_is_package_version(indexed_db):
    from kubedian import __version__
    from kubedian.main import mcp

    async with Client(mcp) as client:
        assert client.initialize_result.serverInfo.version == __version__


async def test_missing_db_clear_error(tmp_path, monkeypatch):
    """A missing index must surface as an actionable message, not a raw traceback."""
    from fastmcp.exceptions import ToolError

    from kubedian.main import mcp
    from kubedian.presentation.tools.dependencies import reset_reader

    monkeypatch.setenv("KUBEDIAN_DB", str(tmp_path / "nope.db"))
    reset_reader()
    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="no index at"):
                await client.call_tool("index_status", {})
            # capabilities still answers, reporting the broken index instead of failing
            res = await client.call_tool("kubedian_capabilities", {})
            assert "no index at" in res.data["index"]["error"]
    finally:
        reset_reader()


async def test_mcp_composition_and_namespace_tools(indexed_db):
    from kubedian.main import mcp

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"service_composition", "namespace_contents"} <= names

        res = await client.call_tool(
            "service_composition", {"service": "gateway", "environment": "staging"}
        )
        assert res.data["namespace"] == "ns-gw"
        egress = {e["target"] for e in res.data["network_policies"]["egress_allowed_to"]}
        assert "service-b" in egress
        assert "ENC[" not in str(res.data)  # never values, only key names

        res = await client.call_tool(
            "namespace_contents", {"namespace": "ns-b", "environment": "staging"}
        )
        assert "service" in res.data["counts"]
        incoming = {e["peer_namespace"] for e in res.data["cross_namespace"]["incoming"]}
        assert "ns-a" in incoming
