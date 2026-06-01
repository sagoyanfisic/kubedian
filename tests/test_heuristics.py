from kubedian.application.heuristics.dns import (
    ClusterTarget,
    is_cluster_internal,
    parse_cluster_host,
)
from kubedian.application.heuristics.env_key_rules import hint_for_key
from kubedian.domain.entities.graph import EdgeType, NodeType


def test_parse_cluster_host_with_port_and_bare():
    assert parse_cluster_host("redis.cache.svc.cluster.local:6379") == ClusterTarget(
        "redis", "cache"
    )


def test_parse_external_host_is_none():
    assert parse_cluster_host("https://api.example.com/v3") is None
    assert is_cluster_internal("https://api.example.com") is False


def test_key_hints_by_family():
    assert hint_for_key("POSTGRES_HOST").edge_type == EdgeType.READS_FROM
    assert hint_for_key("REDIS_URI").edge_type == EdgeType.CACHES_IN
    assert hint_for_key("RABBITMQ_HOST").edge_type == EdgeType.QUEUES_TO
    assert hint_for_key("CELERY_BROKER_URL").edge_type == EdgeType.QUEUES_TO
    assert hint_for_key("AUTH_SERVICE_URL").edge_type == EdgeType.AUTHENTICATES_VIA


def test_generic_url_is_external_low_confidence():
    hint = hint_for_key("BILLING_API_URL")
    assert hint is not None
    assert hint.target_node_type == NodeType.EXTERNAL_API
    assert hint.confidence < 0.5


def test_unrecognised_key_returns_none():
    assert hint_for_key("LOG_LEVEL") is None
    assert hint_for_key("UVICORN_PORT") is None
