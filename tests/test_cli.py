"""CLI tests for the MCP `install` and `doctor` commands (no real claude/servers)."""

import json as _json
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kubedian.application.pipeline.index import index_repo
from kubedian.cli import app
from kubedian.domain.entities.graph import Environment
from tests.conftest import write_sample_repo

runner = CliRunner()


@pytest.fixture()
def fake_claude(monkeypatch):
    """Pretend `claude` exists and record the `claude mcp add` invocation."""
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude" if name == "claude" else None)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("kubedian.cli._mcp_binary", lambda: "/fake/bin/kubedian-mcp")
    return calls


def test_install_defaults_to_local_scope(fake_claude, tmp_path):
    db = tmp_path / "graph.db"
    result = runner.invoke(app, ["install", "--db", str(db)])
    assert result.exit_code == 0
    cmd = fake_claude["cmd"]
    assert cmd[:4] == ["claude", "mcp", "add", "kubedian"]
    assert cmd[4:6] == ["--scope", "local"]
    assert f"KUBEDIAN_DB={db}" in cmd
    # absolute binary path after `--` (the agent's PATH may not include ~/.local/bin)
    assert cmd[cmd.index("--") + 1] == "/fake/bin/kubedian-mcp"


def test_install_scope_user_propagates(fake_claude, tmp_path):
    result = runner.invoke(app, ["install", "--db", str(tmp_path / "graph.db"), "--scope", "user"])
    assert result.exit_code == 0
    assert fake_claude["cmd"][4:6] == ["--scope", "user"]
    assert "every Claude Code session" in result.output


def test_install_rejects_unknown_scope(fake_claude, tmp_path):
    result = runner.invoke(app, ["install", "--db", str(tmp_path / "graph.db"), "--scope", "banana"])
    assert result.exit_code == 2
    assert "unknown scope" in result.output


@pytest.fixture()
def doctor_env(monkeypatch, tmp_path):
    """Stub the environment-dependent checks; the DB and registration checks stay real."""
    monkeypatch.setattr(
        "kubedian.cli._doctor_binary", lambda: ("kubedian-mcp binary", True, "/fake/bin/kubedian-mcp")
    )
    monkeypatch.setattr("kubedian.cli._doctor_fastmcp", lambda: ("fastmcp installed", True, "import ok"))
    monkeypatch.setattr(
        "kubedian.cli._doctor_handshake",
        lambda mcp_bin, db_path, timeout: ("MCP handshake", True, "Kubedian test — 19 tools over stdio"),
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("KUBEDIAN_DB", raising=False)
    monkeypatch.delenv("KUBEDIAN_REPO", raising=False)
    return home


def test_doctor_ok_and_reports_scopes(doctor_env, tmp_path):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    server = {"command": "/fake/bin/kubedian-mcp", "env": {"KUBEDIAN_DB": str(db)}}
    (doctor_env / ".claude.json").write_text(
        _json.dumps(
            {
                "mcpServers": {"kubedian": server},
                "projects": {"/some/project": {"mcpServers": {"kubedian": server}}},
            }
        )
    )

    result = runner.invoke(app, ["doctor", "--db", str(db), "--json"])
    assert result.exit_code == 0
    checks = {c["check"]: c for c in _json.loads(result.output)}
    assert checks["graph DB"]["ok"] is True
    assert "services=" in checks["graph DB"]["detail"]
    reg = checks["Claude Code registration"]
    assert reg["ok"] is True
    assert "scope=user" in reg["detail"] and "scope=local" in reg["detail"]
    assert checks["MCP handshake"]["ok"] is True


def test_doctor_missing_db_fails(doctor_env, tmp_path):
    result = runner.invoke(app, ["doctor", "--db", str(tmp_path / "nope.db"), "--json"])
    assert result.exit_code == 1
    checks = {c["check"]: c for c in _json.loads(result.output)}
    assert checks["graph DB"]["ok"] is False
    assert "no index at" in checks["graph DB"]["detail"]
    # not registered → informational hint, never a hard failure
    assert checks["Claude Code registration"]["ok"] is None
    assert "kubedian install" in checks["Claude Code registration"]["detail"]
