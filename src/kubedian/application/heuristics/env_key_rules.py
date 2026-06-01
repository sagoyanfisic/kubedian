"""Infer dependency *type* from environment-variable / secret key NAMES.

This is the heuristic layer: a key called ``POSTGRES_HOST`` tells us the service
talks to a database, even though the value is encrypted. Every edge produced
here is tagged ``provenance=heuristic`` with a confidence < 1.0 so it is never
presented as a hard fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kubedian.domain.entities.graph import EdgeType, NodeType, Signal


@dataclass(frozen=True)
class KeyHint:
    """A dependency inferred from a key name."""

    edge_type: EdgeType
    target_node_type: NodeType
    # logical datastore/dependency family, used to build a target node id
    family: str
    confidence: float


# Ordered: first matching pattern wins. Patterns are matched case-insensitively
# against the full key name.
_RULES: list[tuple[re.Pattern[str], KeyHint]] = [
    (
        re.compile(r"^(POSTGRES|PG|DATABASE|DB|MYSQL|ALEMBIC)_(HOST|URL|URI|DSN|NAME|DB)$"),
        KeyHint(EdgeType.READS_FROM, NodeType.DATABASE, "postgres", 0.6),
    ),
    (
        re.compile(r"^MONGO(DB)?_(HOST|URL|URI|DB)$"),
        KeyHint(EdgeType.READS_FROM, NodeType.DATABASE, "mongo", 0.6),
    ),
    (
        re.compile(r"^REDIS_(HOST|URL|URI)$"),
        KeyHint(EdgeType.CACHES_IN, NodeType.CACHE, "redis", 0.6),
    ),
    (
        re.compile(r"^(RABBITMQ|AMQP|CELERY)_.*"),
        KeyHint(EdgeType.QUEUES_TO, NodeType.QUEUE, "rabbitmq", 0.55),
    ),
    (
        re.compile(r"^(BROKER|KAFKA)_(URL|URI|HOST|BROKERS)$"),
        KeyHint(EdgeType.QUEUES_TO, NodeType.QUEUE, "broker", 0.55),
    ),
    (
        re.compile(r"^AUTH_SERVICE_URL$"),
        KeyHint(EdgeType.AUTHENTICATES_VIA, NodeType.SERVICE, "auth-service", 0.6),
    ),
]

# Generic "*_URL" keys whose value we can't see: external dependency, low confidence.
_GENERIC_URL = re.compile(r"^[A-Z0-9_]+_(URL|URI|ENDPOINT|API)$")


def hint_for_key(key: str) -> KeyHint | None:
    """Return a typed dependency hint for a key name, or None if unrecognised."""
    name = key.strip().upper()
    for pattern, hint in _RULES:
        if pattern.match(name):
            return hint
    if _GENERIC_URL.match(name):
        return KeyHint(EdgeType.CALLS_EXTERNAL, NodeType.EXTERNAL_API, _slug(name), 0.4)
    return None


def signal_for_hint() -> Signal:
    return Signal.SECRET_KEY_NAME


def _slug(name: str) -> str:
    # Drop the trailing _URL/_URI/... suffix to name the external target.
    base = re.sub(r"_(URL|URI|ENDPOINT|API)$", "", name)
    return base.lower().replace("_", "-")
