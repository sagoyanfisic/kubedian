"""Core graph entities: nodes, edges, provenance.

The graph is the source of truth that every surface (MCP, vault, mermaid, docs)
reads from. Nodes are services and the things services depend on; edges carry
*provenance* so a heuristic edge (inferred from a secret key name) is never
presented as a hard fact.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    SERVICE = "service"
    NAMESPACE = "namespace"
    DATABASE = "database"
    CACHE = "cache"
    QUEUE = "queue"
    EXTERNAL_API = "external_api"
    CONFIGMAP = "configmap"
    SECRET = "secret"
    INGRESS_HOST = "ingress_host"
    HELM_CHART = "helm_chart"
    GATEWAY = "gateway"
    STORAGE = "storage"  # PersistentVolumeClaim
    AUTOSCALER = "autoscaler"  # HorizontalPodAutoscaler
    SERVICE_ACCOUNT = "service_account"
    JOB = "job"  # one-off batch workload
    CRONJOB = "cronjob"  # scheduled batch workload
    ROLE = "role"  # RBAC Role/ClusterRole (names + rule count only)


class EdgeType(StrEnum):
    HTTP_CALLS = "http_calls"
    READS_FROM = "reads_from"
    WRITES_TO = "writes_to"
    CACHES_IN = "caches_in"
    QUEUES_TO = "queues_to"
    AUTHENTICATES_VIA = "authenticates_via"
    CALLS_EXTERNAL = "calls_external"
    ROUTES_TO = "routes_to"
    # Connectivity *permitted* by a NetworkPolicy — declarative but weaker than a
    # call: "X is allowed to reach Y", never "X calls Y". Kept on its own type so
    # consumers never mistake a permission for an observed dependency.
    ALLOWS_TO = "allows_to"
    DEPENDS_ON_CHART = "depends_on_chart"
    REFERENCES = "references"
    OWNS = "owns"
    SELECTS = "selects"
    MOUNTS = "mounts"  # workload -> PersistentVolumeClaim
    SCALES = "scales"  # HorizontalPodAutoscaler -> workload
    RUNS_AS = "runs_as"  # workload -> ServiceAccount
    GRANTS = "grants"  # ServiceAccount -> Role/ClusterRole (via RoleBinding)
    IN_NAMESPACE = "in_namespace"


class Provenance(StrEnum):
    EXPLICIT = "explicit"
    HEURISTIC = "heuristic"
    DOCUMENTED = "documented"  # asserted by human-authored docs (e.g. a Mermaid diagram)


class Signal(StrEnum):
    """How an edge was discovered — drives confidence and citation."""

    CONFIGMAP_URL = "configmap_url"
    DNS_LITERAL = "dns_literal"
    SECRET_KEY_NAME = "secret_key_name"
    ENV_LITERAL = "env_literal"
    HELMCHART = "helmchart"
    ISTIO_VS = "istio_vs"
    INGRESS_BACKEND = "ingress_backend"
    NETWORK_POLICY = "network_policy"
    VOLUME_CLAIM = "volume_claim"
    HPA_TARGET = "hpa_target"
    KEDA_SCALER = "keda_scaledobject"
    SERVICE_ACCOUNT_REF = "service_account_ref"
    ROLE_BINDING = "role_binding"
    EXTERNAL_NAME = "external_name"  # Service of type ExternalName
    GATEWAY_BINDING = "gateway_binding"
    AUTH_ANNOTATION = "auth_annotation"
    OWNER_REF = "owner_ref"
    SELECTOR = "selector"
    NAMESPACE = "namespace"
    VOLUME_MOUNT = "volume_mount"
    ENV_FROM = "env_from"  # whole Secret/ConfigMap injected via envFrom
    ENV_KEY_REF = "env_key_ref"  # single key via env[].valueFrom.{secret,configMap}KeyRef
    DOC_MERMAID = "doc_mermaid"  # parsed from a Mermaid diagram in the docs


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class Node(BaseModel):
    id: str
    type: NodeType
    name: str
    namespace: str | None = None
    attrs: dict = Field(default_factory=dict)


class Edge(BaseModel):
    src_id: str
    dst_id: str
    type: EdgeType
    environment: Environment
    provenance: Provenance
    signal: Signal
    source_file: str | None = None
    # Only ever a *key name* / field path — never a secret value.
    source_locator: str | None = None
    confidence: float = 1.0
    attrs: dict = Field(default_factory=dict)

    @property
    def key(self) -> tuple:
        """Dedup identity: same logical edge in the same environment."""
        return (self.src_id, self.dst_id, self.type, self.environment)


class Graph(BaseModel):
    nodes: dict[str, Node] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        # Merge attrs, keep first-seen type/name.
        existing.attrs.update(node.attrs)
        if existing.namespace is None and node.namespace is not None:
            existing.namespace = node.namespace
        return existing

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
