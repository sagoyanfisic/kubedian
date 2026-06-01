"""Enrich the graph with edges asserted by human-authored docs.

Parses Mermaid ``flowchart``/``graph`` blocks in Markdown files and adds the
edges they declare with ``provenance=documented``. This captures topology that
is otherwise invisible to static manifest analysis — external SaaS calls,
cross-cluster links, and dependencies whose addresses live in encrypted secrets.

Doc endpoints are matched to existing service nodes by name; unmatched endpoints
(PostgreSQL, RabbitMQ, external APIs / SaaS…) become typed "documented" nodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kubedian.domain.entities.graph import (
    Edge,
    EdgeType,
    Environment,
    Graph,
    Node,
    NodeType,
    Provenance,
    Signal,
)

_DOC_CONFIDENCE = 0.9

# Mermaid node declaration: ID["label"] / ID(["label"]) / ID[("label")] / ID{{"label"}} ...
_NODE_DECL = re.compile(
    r'(?P<id>[A-Za-z0-9_]+)\s*[\[\(\{>]{1,2}"?(?P<label>[^"\]\)\}]+?)"?[\]\)\}]{1,2}'
)
# Mermaid edge: A -->|label| B  /  A --- B  /  A -.->|x| B  /  A ==> B
_EDGE = re.compile(
    r'(?P<src>[A-Za-z0-9_]+)\s*'
    r'(?P<arrow>-{2,3}>|-{1,2}\.-?->|={2,3}>|-{3}|---)\s*'
    r'(?:\|"?(?P<label>[^|]*?)"?\|\s*)?'
    r'(?P<dst>[A-Za-z0-9_]+)'
)

_QUEUE_KW = ("publish", "consume", "queue", "event", "amqp", "rabbit", "mq", "kafka")
_AUTH_KW = ("auth", "oauth", "login", "sso")
_EXTERNAL_KW = ("aws", "sdk", "saas", "webhook", "cdn")
_DB_KW = ("postgres", "postgresql", "mysql", "mariadb", "rds", "database", " db", "sql")
_CACHE_KW = ("redis", "cache", "memcached")


@dataclass
class _DocEdge:
    src: str  # resolved label
    dst: str
    label: str
    directed: bool


def ingest_docs(graph: Graph, docs_dir: Path, environments: list[Environment]) -> int:
    """Parse every ``*.md`` under ``docs_dir`` and add documented edges.

    Returns the number of edges added. Edges are added for every environment in
    ``environments`` (docs describe topology, not a single overlay)."""
    if not docs_dir.is_dir():
        return 0
    service_index = _service_lookup(graph)
    added = 0
    for md in sorted(docs_dir.rglob("*.md")):
        for diagram in _mermaid_blocks(md.read_text(encoding="utf-8", errors="ignore")):
            for de in _parse_diagram(diagram):
                added += _add_doc_edge(graph, service_index, de, md, environments)
    return added


# --------------------------------------------------------------------------- #
def _mermaid_blocks(text: str) -> list[str]:
    blocks = re.findall(r"```mermaid\s*(.*?)```", text, re.DOTALL)
    return [b for b in blocks if re.search(r"\b(flowchart|graph)\b", b)]


def _parse_diagram(diagram: str) -> list[_DocEdge]:
    labels: dict[str, str] = {}
    for m in _NODE_DECL.finditer(diagram):
        labels[m.group("id")] = _clean_label(m.group("label"))

    edges: list[_DocEdge] = []
    for m in _EDGE.finditer(diagram):
        src_id, dst_id = m.group("src"), m.group("dst")
        # skip mermaid keywords that the loose regex may catch
        if src_id in ("subgraph", "end", "flowchart", "graph") or dst_id in ("end",):
            continue
        src = labels.get(src_id, _clean_label(src_id))
        dst = labels.get(dst_id, _clean_label(dst_id))
        directed = ">" in m.group("arrow")
        edges.append(_DocEdge(src=src, dst=dst, label=(m.group("label") or "").strip(), directed=directed))
    return edges


def _clean_label(raw: str) -> str:
    # take the first line (Mermaid uses literal \n or <br>), drop parentheticals
    first = re.split(r"\\n|<br\s*/?>|\n", raw)[0]
    first = re.sub(r"\(.*?\)", "", first)
    return first.strip().strip('"').strip()


# --------------------------------------------------------------------------- #
def _service_lookup(graph: Graph) -> dict[str, str]:
    """name(lower) -> node id, for service nodes only."""
    out: dict[str, str] = {}
    for n in graph.nodes.values():
        if n.type == NodeType.SERVICE:
            out[n.name.lower()] = n.id
    return out


def _match_service(label: str, index: dict[str, str]) -> str | None:
    norm = label.lower().strip()
    if not norm:
        return None
    if norm in index:
        return index[norm]
    # prefix match either direction (e.g. "auth" ~ "auth-service")
    best: tuple[int, str] | None = None
    for name, nid in index.items():
        if len(norm) < 4 or len(name) < 4:
            continue
        if name.startswith(norm) or norm.startswith(name):
            score = min(len(name), len(norm))
            if best is None or score > best[0]:
                best = (score, nid)
    return best[1] if best else None


def _classify(de: _DocEdge, dst_label: str) -> tuple[EdgeType, NodeType]:
    text = f"{de.label} {dst_label}".lower()
    if any(k in text for k in _QUEUE_KW):
        return EdgeType.QUEUES_TO, NodeType.QUEUE
    if not de.directed:  # association line, usually a datastore
        if any(k in text for k in _CACHE_KW):
            return EdgeType.CACHES_IN, NodeType.CACHE
        if any(k in text for k in _DB_KW):
            return EdgeType.READS_FROM, NodeType.DATABASE
        return EdgeType.REFERENCES, NodeType.EXTERNAL_API
    if any(k in de.label.lower() for k in _AUTH_KW):
        return EdgeType.AUTHENTICATES_VIA, NodeType.SERVICE
    if any(k in text for k in _EXTERNAL_KW):
        return EdgeType.CALLS_EXTERNAL, NodeType.EXTERNAL_API
    if any(k in text for k in _CACHE_KW):
        return EdgeType.CACHES_IN, NodeType.CACHE
    if any(k in text for k in _DB_KW):
        return EdgeType.READS_FROM, NodeType.DATABASE
    return EdgeType.HTTP_CALLS, NodeType.SERVICE


def _add_doc_edge(
    graph: Graph,
    index: dict[str, str],
    de: _DocEdge,
    md: Path,
    environments: list[Environment],
) -> int:
    if not de.src or not de.dst or de.src == de.dst:
        return 0
    edge_type, fallback_type = _classify(de, de.dst)

    src_id = _match_service(de.src, index)
    if src_id is None:
        return 0  # only emit edges that originate from a known service

    dst_id = _match_service(de.dst, index)
    if dst_id is None:
        # Never invent a new "service" from a doc label (avoids domain-group noise
        # like "Client domain"). Unmatched service-ish targets are external systems.
        if fallback_type == NodeType.SERVICE:
            fallback_type = NodeType.EXTERNAL_API
        dst_id = _doc_node(graph, de.dst, fallback_type)
    if src_id == dst_id:
        return 0

    added = 0
    for env in environments:
        graph.add_edge(
            Edge(
                src_id=src_id,
                dst_id=dst_id,
                type=edge_type,
                environment=env,
                provenance=Provenance.DOCUMENTED,
                signal=Signal.DOC_MERMAID,
                source_file=str(md),
                source_locator=de.label or None,
                confidence=_DOC_CONFIDENCE,
            )
        )
        added += 1
    return added


def _doc_node(graph: Graph, label: str, node_type: NodeType) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "unknown"
    prefix = {
        NodeType.DATABASE: "db",
        NodeType.CACHE: "cache",
        NodeType.QUEUE: "queue",
        NodeType.EXTERNAL_API: "ext",
        NodeType.SERVICE: "svc-doc",
    }.get(node_type, "doc")
    node_id = f"{prefix}:doc/{slug}"
    graph.add_node(Node(id=node_id, type=node_type, name=label, attrs={"documented": True}))
    return node_id
