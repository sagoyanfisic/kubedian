from pathlib import Path

from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.application.pipeline.resolve import resolve
from kubedian.domain.entities.graph import EdgeType, Environment, NodeType, Provenance
from tests.conftest import write_sample_repo


def _graph_for(repo: Path):
    overlays = discover_overlays(repo, Environment.STAGING)
    results = [extract_overlay(o) for o in overlays]
    return resolve(results)


def test_explicit_http_call_from_configmap(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    http = [e for e in graph.edges if e.type == EdgeType.HTTP_CALLS]
    assert any(
        e.src_id == "svc:ns-a/service-a"
        and e.dst_id == "svc:ns-b/service-b"
        and e.provenance == Provenance.EXPLICIT
        and "SERVICE_B_API_URL" in (e.source_locator or "")
        for e in http
    ), [(e.src_id, e.dst_id, e.source_locator) for e in http]


def test_target_node_not_duplicated(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    service_b_nodes = [n for n in graph.nodes.values() if n.name == "service-b"]
    assert len(service_b_nodes) == 1
    assert service_b_nodes[0].id == "svc:ns-b/service-b"


def test_secret_key_heuristics_emit_datastores(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    out = {e.type for e in graph.edges if e.src_id == "svc:ns-a/service-a"}
    assert EdgeType.READS_FROM in out  # POSTGRES_HOST
    assert EdgeType.CACHES_IN in out  # REDIS_URI
    assert EdgeType.QUEUES_TO in out  # RABBITMQ_*
    # heuristic edges must be tagged as such, never explicit
    for e in graph.edges:
        if e.type in (EdgeType.READS_FROM, EdgeType.CACHES_IN, EdgeType.QUEUES_TO):
            assert e.provenance == Provenance.HEURISTIC
            assert e.confidence < 1.0


def test_rabbitmq_keys_dedup_to_single_queue_edge(tmp_path):
    """RABBITMQ_HOST + RABBITMQ_PORT must collapse to one queues_to edge."""
    graph = _graph_for(write_sample_repo(tmp_path))
    queue_edges = [
        e
        for e in graph.edges
        if e.src_id == "svc:ns-a/service-a" and e.type == EdgeType.QUEUES_TO
    ]
    assert len(queue_edges) == 1
    locators = queue_edges[0].attrs.get("locators") or []
    assert "RABBITMQ_HOST" in locators and "RABBITMQ_PORT" in locators


def test_namespace_membership_edges(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    ns_edges = [e for e in graph.edges if e.type == EdgeType.IN_NAMESPACE]
    assert any(e.src_id == "svc:ns-a/service-a" and e.dst_id == "ns:ns-a" for e in ns_edges)
    assert NodeType.NAMESPACE in {n.type for n in graph.nodes.values()}
