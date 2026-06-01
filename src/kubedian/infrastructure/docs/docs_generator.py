"""Generate trilingual architecture docs (Markdown) from the graph.

Produces an index, one page per service and one per namespace, each embedding a
Mermaid diagram and citing edge provenance. Strings come from the i18n catalogs;
graph-derived content (names, files) is language-neutral.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from kubedian.infrastructure.mermaid import mermaid_renderer
from kubedian.infrastructure.sanitize import assert_no_secret_values
from kubedian.domain.entities.graph import Edge, EdgeType, Graph, Node, NodeType
from kubedian.i18n import t

_EDGE_KEY = {
    EdgeType.HTTP_CALLS: "http_calls",
    EdgeType.READS_FROM: "reads_from",
    EdgeType.WRITES_TO: "writes_to",
    EdgeType.CACHES_IN: "caches_in",
    EdgeType.QUEUES_TO: "queues_to",
    EdgeType.AUTHENTICATES_VIA: "authenticates_via",
    EdgeType.CALLS_EXTERNAL: "calls_external",
    EdgeType.DEPENDS_ON_CHART: "depends_on_chart",
    EdgeType.ROUTES_TO: "routes_to",
}

_DATASTORE_TYPES = {NodeType.DATABASE, NodeType.CACHE, NodeType.QUEUE}


def export_docs(graph: Graph, out_dir: Path, *, lang: str = "en") -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_edges: dict[str, list[Edge]] = defaultdict(list)
    in_edges: dict[str, list[Edge]] = defaultdict(list)
    for e in graph.edges:
        if e.type == EdgeType.IN_NAMESPACE:
            continue
        out_edges[e.src_id].append(e)
        in_edges[e.dst_id].append(e)

    services = sorted(
        (n for n in graph.nodes.values() if n.type == NodeType.SERVICE),
        key=lambda n: (n.namespace or "", n.name),
    )

    pages = 0
    _write(out_dir / "index.md", _index(services, graph, lang))
    pages += 1
    services_dir = out_dir / "services"
    services_dir.mkdir(exist_ok=True)
    for svc in services:
        _write(
            services_dir / f"{_slug(svc)}.md",
            _service_page(svc, graph, out_edges[svc.id], in_edges[svc.id], lang),
        )
        pages += 1
    return pages


def _index(services: list[Node], graph: Graph, lang: str) -> str:
    by_ns: dict[str, list[Node]] = defaultdict(list)
    for s in services:
        by_ns[s.namespace or "—"].append(s)
    lines = [f"# {t(lang, 'docs', 'index_title')}", ""]
    for ns in sorted(by_ns):
        lines.append(f"## {t(lang, 'docs', 'namespace')}: `{ns}`")
        for s in by_ns[ns]:
            lines.append(f"- [{s.name}](services/{_slug(s)}.md)")
        lines.append("")
    lines += [f"## {t(lang, 'docs', 'title')}", "", "```mermaid",
              mermaid_renderer.render_flowchart(graph, lang=lang).rstrip(), "```", ""]
    return "\n".join(lines)


def _service_page(
    svc: Node, graph: Graph, out_edges: list[Edge], in_edges: list[Edge], lang: str
) -> str:
    lines = [f"# {t(lang, 'docs', 'service_page_title', service=svc.name)}", ""]
    lines.append(f"- **{t(lang, 'docs', 'namespace')}**: `{svc.namespace or '—'}`")
    if svc.attrs.get("image"):
        lines.append(f"- **{t(lang, 'docs', 'image')}**: `{svc.attrs['image']}`")
    lines.append("")

    calls = [e for e in out_edges if e.type == EdgeType.HTTP_CALLS]
    datastores = [e for e in out_edges if graph.nodes.get(e.dst_id) and graph.nodes[e.dst_id].type in _DATASTORE_TYPES]
    external = [e for e in out_edges if e.type == EdgeType.CALLS_EXTERNAL]

    lines += _edge_section(t(lang, "docs", "outgoing"), calls, graph, lang)
    lines += _edge_section(t(lang, "docs", "incoming"),
                           [e for e in in_edges if e.type == EdgeType.HTTP_CALLS], graph, lang, incoming=True)
    lines += _edge_section(t(lang, "docs", "datastores"), datastores, graph, lang)
    lines += _edge_section(t(lang, "docs", "external"), external, graph, lang)

    lines += [f"## {t(lang, 'docs', 'diagram')}", "", "```mermaid",
              mermaid_renderer.render_focus(graph, svc.id, lang=lang).rstrip(), "```", ""]
    return "\n".join(lines)


def _edge_section(title: str, edges: list[Edge], graph: Graph, lang: str, incoming: bool = False) -> list[str]:
    lines = [f"## {title}", ""]
    if not edges:
        lines += [f"_{t(lang, 'docs', 'none')}_", ""]
        return lines
    for e in edges:
        other_id = e.src_id if incoming else e.dst_id
        other = graph.nodes.get(other_id)
        name = other.name if other else other_id
        verb_key = _EDGE_KEY.get(e.type, e.type.value)
        verb = t(lang, "edge", verb_key)
        prov = (
            t(lang, "docs", "provenance_explicit")
            if e.provenance.value == "explicit"
            else t(lang, "docs", "provenance_heuristic")
        )
        cite = f" — _{prov}_"
        if e.source_file:
            cite += f" ({t(lang, 'docs', 'source')}: `{_relpath(e.source_file)}`)"
        lines.append(f"- {verb} **{name}**{cite}")
    lines.append("")
    return lines


def _write(path: Path, text: str) -> None:
    assert_no_secret_values(text)
    path.write_text(text, encoding="utf-8")


def _slug(node: Node) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in node.name).strip("-").lower()


def _relpath(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else path
