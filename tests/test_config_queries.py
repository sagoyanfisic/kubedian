"""service_secrets / service_ports / find_key_usage / find_port use-cases.

Everything here must surface NAMES and relations only — never a secret value.
"""

import json

import pytest

from kubedian.application.pipeline.index import index_repo
from kubedian.application.use_cases import queries
from kubedian.domain.entities.graph import Environment
from kubedian.infrastructure.sanitize import assert_no_secret_values
from kubedian.infrastructure.sqlite.graph_reader import GraphReader
from tests.conftest import write_sample_repo


@pytest.fixture()
def reader(tmp_path) -> GraphReader:
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    r = GraphReader(db)
    yield r
    r.close()


def test_service_secrets_lists_keys_and_modes(reader):
    out = queries.service_secrets(reader, "service-a", Environment.STAGING)
    assert out["service"]["name"] == "service-a"
    secrets = {s["name"]: s for s in out["secrets"]}
    assert "service-a-secret" in secrets
    entry = secrets["service-a-secret"]
    assert set(entry["modes"]) == {"env_from", "env_key_ref"}
    assert "POSTGRES_HOST" in entry["keys"]
    assert entry["env_map"] == {"DATABASE_HOST": "POSTGRES_HOST"}
    configmaps = {c["name"] for c in out["configmaps"]}
    assert {"service-a-config", "service-discovery"} <= configmaps


def test_service_secrets_never_returns_values(reader):
    out = queries.service_secrets(reader, "service-a", Environment.STAGING)
    blob = json.dumps(out)
    assert_no_secret_values(blob)
    assert "ENC[" not in blob


def test_service_ports_full_wiring(reader):
    out = queries.service_ports(reader, "service-b", Environment.STAGING)
    assert out["container_ports"] == [8080]
    assert out["named_ports"] == {"http": 8080}
    wiring = {(p["service"], p["port"], p["target_port"]) for p in out["service_ports"]}
    assert ("service-b", 80, 8080) in wiring
    # exposed via the gateway's VirtualService, destination port included
    vs = [r for r in out["exposed_via"] if r.get("port") == 8080]
    assert vs, out["exposed_via"]


def test_service_ports_filled_from_service(reader):
    """service-a's workload declares no containerPort; ports come from its Service."""
    out = queries.service_ports(reader, "service-a", Environment.STAGING)
    assert out["container_ports"] == [8000]
    assert any(p["port"] == 80 and p["target_port"] == 8000 for p in out["service_ports"])


def test_find_key_usage_exact(reader):
    out = queries.find_key_usage(reader, "POSTGRES_HOST", None)
    assert out["match_count"] >= 1
    by_mode = {m["mode"]: m for m in out["matches"] if m["workload"] == "service-a"}
    # wired to the DATABASE_HOST env var via valueFrom...
    assert by_mode["env_key_ref"]["var"] == "DATABASE_HOST"
    assert by_mode["env_key_ref"]["key"] == "POSTGRES_HOST"
    assert by_mode["env_key_ref"]["ref"] == "service-a-secret"
    # ...and injected as-is via envFrom (var == key)
    assert by_mode["env_from"]["var"] == "POSTGRES_HOST"
    assert all(m["environment"] == "staging" for m in out["matches"])


def test_find_key_usage_matches_var_name(reader):
    """Searching by the env var NAME (not the secret key) must also hit."""
    out = queries.find_key_usage(reader, "DATABASE_HOST", None)
    assert any(
        m["var"] == "DATABASE_HOST" and m["key"] == "POSTGRES_HOST"
        for m in out["matches"]
    )


def test_find_key_usage_partial(reader):
    out = queries.find_key_usage(reader, "rabbitmq", None, partial=True)
    keys = {m["key"] for m in out["matches"]}
    assert {"RABBITMQ_HOST", "RABBITMQ_PORT"} <= keys


def test_find_key_usage_never_returns_values(reader):
    for q, partial in (("POSTGRES_HOST", False), ("_", True)):
        blob = json.dumps(queries.find_key_usage(reader, q, None, partial=partial))
        assert_no_secret_values(blob)
        assert "ENC[" not in blob


def test_find_port_listeners_and_routes(reader):
    out = queries.find_port(reader, 8080, None)
    listeners = {n["name"] for n in out["listeners"]}
    assert "service-b" in listeners
    assert any(r["target"] == "service-b" for r in out["routed_by"])


def test_find_port_matches_service_port(reader):
    """Port 80 only exists as a Service port (port_map), not a containerPort."""
    out = queries.find_port(reader, 80, None)
    listeners = {n["name"] for n in out["listeners"]}
    assert "service-b" in listeners
