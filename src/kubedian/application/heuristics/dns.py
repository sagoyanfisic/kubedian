"""Parse Kubernetes internal DNS names into (service, namespace).

Cluster-internal service URLs look like
``http://catalog-service.catalog.svc.cluster.local`` →
service ``catalog-service`` in namespace ``catalog``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# <service>.<namespace>.svc.cluster.local  (port optional, handled by caller)
_CLUSTER_DNS = re.compile(
    r"^(?P<service>[a-z0-9][a-z0-9-]*)\.(?P<namespace>[a-z0-9][a-z0-9-]*)"
    r"\.svc(\.cluster\.local)?$"
)


@dataclass(frozen=True)
class ClusterTarget:
    service: str
    namespace: str


def parse_cluster_host(value: str) -> ClusterTarget | None:
    """Return the cluster target if ``value`` points at an in-cluster service.

    Accepts a bare host or a full URL. Returns ``None`` for external hosts.
    """
    host = extract_host(value)
    if host is None:
        return None
    m = _CLUSTER_DNS.match(host)
    if not m:
        return None
    return ClusterTarget(service=m.group("service"), namespace=m.group("namespace"))


def is_cluster_internal(value: str) -> bool:
    host = extract_host(value)
    return host is not None and ".svc" in host


def extract_host(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if "://" in value:
        parsed = urlparse(value)
        return parsed.hostname
    # bare host[:port]
    host = value.split("/", 1)[0]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host or None
