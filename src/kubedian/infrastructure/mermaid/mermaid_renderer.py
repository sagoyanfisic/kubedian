"""Render a graph (or a focused slice) as MermaidJS text.

Architecture as text — no rendering engine of our own, just Mermaid source that
Obsidian, GitHub and mermaid.live draw. Node shape/class encodes type; edge
style encodes provenance (solid = explicit, dotted = heuristic).
"""

from __future__ import annotations

from kubedian.domain.entities.graph import Edge, EdgeType, Graph, Node, NodeType
from kubedian.i18n import t

_CLASSDEF = {
    NodeType.SERVICE: "classDef service fill:#dbeafe,stroke:#2563eb,color:#1e3a8a;",
    NodeType.DATABASE: "classDef database fill:#dcfce7,stroke:#16a34a,color:#14532d;",
    NodeType.CACHE: "classDef cache fill:#fee2e2,stroke:#dc2626,color:#7f1d1d;",
    NodeType.QUEUE: "classDef queue fill:#fef9c3,stroke:#ca8a04,color:#713f12;",
    NodeType.EXTERNAL_API: "classDef external_api fill:#f3e8ff,stroke:#9333ea,color:#581c87;",
    NodeType.HELM_CHART: "classDef helm_chart fill:#e0f2fe,stroke:#0284c7,color:#075985;",
    NodeType.CONFIGMAP: "classDef configmap fill:#f1f5f9,stroke:#64748b,color:#334155;",
    NodeType.SECRET: "classDef secret fill:#f1f5f9,stroke:#64748b,color:#334155;",
}

# Mermaid node shapes per type: service in rounded box, datastores in cylinders.
_SHAPE = {
    NodeType.SERVICE: ("([", "])"),
    NodeType.DATABASE: ("[(", ")]"),
    NodeType.CACHE: ("[(", ")]"),
    NodeType.QUEUE: (">", "]"),
    NodeType.EXTERNAL_API: ("{{", "}}"),
    NodeType.HELM_CHART: ("[/", "/]"),
    NodeType.NAMESPACE: ("[", "]"),
    NodeType.CONFIGMAP: ("[\\", "\\]"),
    NodeType.SECRET: ("[\\", "\\]"),
}

_EDGE_LABEL_KEY = {
    EdgeType.HTTP_CALLS: "http_calls",
    EdgeType.READS_FROM: "reads_from",
    EdgeType.WRITES_TO: "writes_to",
    EdgeType.CACHES_IN: "caches_in",
    EdgeType.QUEUES_TO: "queues_to",
    EdgeType.AUTHENTICATES_VIA: "authenticates_via",
    EdgeType.CALLS_EXTERNAL: "calls_external",
    EdgeType.ROUTES_TO: "routes_to",
    EdgeType.DEPENDS_ON_CHART: "depends_on_chart",
    EdgeType.REFERENCES: "references",
}

# Edge types never drawn in the topology view (structural / config plumbing).
_HIDDEN_EDGES = {EdgeType.IN_NAMESPACE}


def render_flowchart(
    graph: Graph, *, lang: str = "en", group_by_namespace: bool = True, include_references: bool = False
) -> str:
    """Render a flowchart of the graph. ``in_namespace`` edges are not drawn —
    namespaces become ``subgraph`` containers instead. ``references`` (config
    mounts) are hidden in the topology view unless ``include_references``."""
    hidden = set(_HIDDEN_EDGES)
    if not include_references:
        hidden.add(EdgeType.REFERENCES)
    edges = [e for e in graph.edges if e.type not in hidden]
    used_ids = {e.src_id for e in edges} | {e.dst_id for e in edges}
    nodes = {nid: graph.nodes[nid] for nid in used_ids if nid in graph.nodes}

    lines: list[str] = ["flowchart LR"]
    safe = _id_map(nodes)

    if group_by_namespace:
        lines += _namespace_subgraphs(nodes, safe)
    else:
        for nid, node in nodes.items():
            lines.append("    " + _node_line(node, safe[nid]))

    for e in edges:
        if e.src_id not in safe or e.dst_id not in safe:
            continue
        lines.append("    " + _edge_line(e, safe, lang))

    lines += _classdefs(nodes, safe)
    return "\n".join(lines) + "\n"


def render_focus(graph: Graph, focus_id: str, *, lang: str = "en") -> str:
    """One service plus its direct callers and callees."""
    keep = {focus_id}
    for e in graph.edges:
        if e.type == EdgeType.IN_NAMESPACE:
            continue
        if e.src_id == focus_id:
            keep.add(e.dst_id)
        if e.dst_id == focus_id:
            keep.add(e.src_id)
    sub = Graph()
    for nid in keep:
        if nid in graph.nodes:
            sub.add_node(graph.nodes[nid])
    sub.edges = [
        e
        for e in graph.edges
        if e.type != EdgeType.IN_NAMESPACE and (e.src_id == focus_id or e.dst_id == focus_id)
    ]
    return render_flowchart(sub, lang=lang, group_by_namespace=False, include_references=True)


# --------------------------------------------------------------------------- #
def _id_map(nodes: dict[str, Node]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, nid in enumerate(nodes):
        out[nid] = f"n{i}"
    return out


def _node_line(node: Node, safe_id: str) -> str:
    open_b, close_b = _SHAPE.get(node.type, ("[", "]"))
    label = _escape(node.name)
    line = f"{safe_id}{open_b}\"{label}\"{close_b}"
    if node.type in _CLASSDEF:
        line += f":::{node.type.value}"
    return line


def _edge_line(edge: Edge, safe: dict[str, str], lang: str) -> str:
    src, dst = safe[edge.src_id], safe[edge.dst_id]
    label_key = _EDGE_LABEL_KEY.get(edge.type)
    label = t(lang, "edge", label_key) if label_key else edge.type.value
    prov = edge.provenance.value
    arrow = "-.->" if prov == "heuristic" else "==>" if prov == "documented" else "-->"
    return f'{src} {arrow}|{_escape(label)}| {dst}'


def _namespace_subgraphs(nodes: dict[str, Node], safe: dict[str, str]) -> list[str]:
    lines: list[str] = []
    grouped: dict[str, list[str]] = {}
    loose: list[str] = []
    for nid, node in nodes.items():
        if node.type == NodeType.SERVICE and node.namespace:
            grouped.setdefault(node.namespace, []).append(nid)
        else:
            loose.append(nid)
    for ns, ids in sorted(grouped.items()):
        lines.append(f'    subgraph ns_{_escape_id(ns)}["{_escape(ns)}"]')
        for nid in ids:
            lines.append("        " + _node_line(nodes[nid], safe[nid]))
        lines.append("    end")
    for nid in loose:
        lines.append("    " + _node_line(nodes[nid], safe[nid]))
    return lines


def _classdefs(nodes: dict[str, Node], safe: dict[str, str]) -> list[str]:
    present = {n.type for n in nodes.values() if n.type in _CLASSDEF}
    return ["    " + _CLASSDEF[t] for t in present]


def _escape(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _escape_id(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text)
