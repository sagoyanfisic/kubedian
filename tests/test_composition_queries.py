"""service_composition / namespace_contents use-cases + namespace filters.

Everything here must surface NAMES and relations only — never a secret value.
"""

import json

import pytest

from kubedian.application.pipeline.index import index_repo
from kubedian.application.use_cases import queries
from kubedian.domain.entities.graph import Environment
from kubedian.infrastructure.sanitize import assert_no_secret_values
from kubedian.infrastructure.sqlite.graph_reader import GraphReader
from tests.conftest import GATEWAY_SECRET_VALUE, repo_writer, write_sample_repo


@pytest.fixture()
def reader(tmp_path) -> GraphReader:
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    r = GraphReader(db)
    yield r
    r.close()


def test_composition_surfaces_network_policies_both_directions(reader):
    out = queries.service_composition(reader, "gateway", Environment.STAGING)
    assert out["namespace"] == "ns-gw"
    egress = {e["target"] for e in out["network_policies"]["egress_allowed_to"]}
    ingress = {e["service"] for e in out["network_policies"]["ingress_allowed_from"]}
    assert "service-b" in egress
    assert "service-a" in ingress
    # exposure: the Ingress host routes to the gateway
    assert any(e["service"] == "gw.example.com" for e in out["exposure"])
    # config consumed (generator-synthesised) with key names only
    secrets = {s["name"]: s for s in out["config"]["secrets"]}
    assert "POSTGRES_HOST" in secrets["gateway-sc"]["keys"]


def test_composition_surfaces_storage_autoscaling_identity(reader):
    out = queries.service_composition(reader, "data-store", Environment.STAGING)
    assert out["service"]["workload_kind"] == "StatefulSet"
    assert [s["target"] for s in out["storage"]] == ["data"]
    assert [a["autoscaler"] for a in out["autoscaling"]] == ["data-store"]
    assert [i["target"] for i in out["service_account"]] == ["data-sa"]
    # full detail only here: containers list with role tagging
    assert out["service"]["containers"][0] == {"name": "db", "role": "main", "image": "example/data-store:latest"}


def test_composition_never_returns_values(reader):
    for svc in ("gateway", "service-a", "data-store"):
        blob = json.dumps(queries.service_composition(reader, svc, Environment.STAGING))
        assert_no_secret_values(blob)
        assert "ENC[" not in blob
        assert GATEWAY_SECRET_VALUE not in blob


def test_namespace_contents_groups_and_counts(reader):
    out = queries.namespace_contents(reader, "ns-b", Environment.STAGING)
    assert out["counts"]["service"] >= 1
    names = {n["name"] for n in out["contents"]["service"]}
    assert "service-b" in names
    blob = json.dumps(out)
    assert_no_secret_values(blob)
    assert GATEWAY_SECRET_VALUE not in blob


def test_namespace_contents_cross_namespace_edges(reader):
    out = queries.namespace_contents(reader, "ns-b", Environment.STAGING)
    incoming = {(e["peer_namespace"], e["edge_type"]) for e in out["cross_namespace"]["incoming"]}
    # service-a (ns-a) calls service-b; the gateway (ns-gw) routes to and is
    # allowed to reach it.
    assert ("ns-a", "http_calls") in incoming
    assert ("ns-gw", "routes_to") in incoming
    assert ("ns-gw", "allows_to") in incoming
    for entry in out["cross_namespace"]["incoming"]:
        assert entry["count"] >= 1
        assert 1 <= len(entry["examples"]) <= 3


def test_namespace_contents_unknown_namespace(reader):
    assert "error" in queries.namespace_contents(reader, "nope", None)


def test_namespace_filters(reader):
    # search restricted to one namespace
    out = queries.search(reader, "service", namespace="ns-b")
    assert {n["namespace"] for n in out["results"]} == {"ns-b"}
    # POSTGRES_HOST exists in ns-a (service-a-secret) and ns-gw (generator secret)
    all_ns = {m["workload"] for m in queries.find_key_usage(reader, "POSTGRES_HOST", None)["matches"]}
    assert {"service-a", "gateway"} <= all_ns
    only_a = queries.find_key_usage(reader, "POSTGRES_HOST", None, namespace="ns-a")["matches"]
    assert {m["workload"] for m in only_a} == {"service-a"}
    # port 80 is served in ns-a and ns-b; the filter keeps one side
    listeners = {n["name"] for n in queries.find_port(reader, 80, None, namespace="ns-b")["listeners"]}
    assert "service-b" in listeners and "service-a" not in listeners


def test_composition_bundle_and_rbac(tmp_path):
    """An api+worker bundle: composition of the api lists the worker as a bundle
    sibling, and the roles granted to its ServiceAccount appear under rbac."""
    repo = tmp_path / "r"
    w = repo_writer(repo)
    w(
        "billing/base/api.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: billing
          namespace: placeholder
          labels: {app: billing}
        spec:
          template:
            metadata:
              labels: {app: billing, component: api}
            spec:
              serviceAccountName: billing-sa
              containers:
                - name: api
                  image: x:1
        """,
    )
    w(
        "billing/base/worker.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: billing-worker
          namespace: placeholder
          labels: {app: billing}
        spec:
          template:
            metadata:
              labels: {app: billing, component: worker}
            spec:
              containers:
                - name: worker
                  image: x:1
        """,
    )
    w(
        "billing/base/rbac.yaml",
        """
        apiVersion: v1
        kind: ServiceAccount
        metadata:
          name: billing-sa
          namespace: placeholder
        ---
        apiVersion: rbac.authorization.k8s.io/v1
        kind: Role
        metadata:
          name: billing-reader
          namespace: placeholder
        rules:
          - apiGroups: [""]
            resources: [configmaps]
            verbs: [get]
        ---
        apiVersion: rbac.authorization.k8s.io/v1
        kind: RoleBinding
        metadata:
          name: billing-reader-binding
          namespace: placeholder
        roleRef: {kind: Role, name: billing-reader, apiGroup: rbac.authorization.k8s.io}
        subjects:
          - {kind: ServiceAccount, name: billing-sa}
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    reader = GraphReader(db)
    try:
        out = queries.service_composition(reader, "svc:billing/billing", Environment.STAGING)
        assert [b["name"] for b in out["bundle"]] == ["billing-worker"]
        (grant,) = out["rbac"]
        assert grant["service_account"] == "billing-sa"
        assert grant["role"] == "billing-reader"
        assert grant["cluster_wide"] is False
    finally:
        reader.close()
