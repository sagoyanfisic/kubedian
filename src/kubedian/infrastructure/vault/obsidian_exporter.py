"""Export the graph as an Obsidian vault: one note per node, [[wikilinks]] per
edge, tags per type, and an embedded Mermaid focus diagram. Opening the folder
in Obsidian gives a force-directed graph for free."""

from __future__ import annotations

from pathlib import Path

from kubedian.infrastructure.mermaid import mermaid_renderer
from kubedian.infrastructure.sanitize import assert_no_secret_values
from kubedian.domain.entities.graph import Edge, EdgeType, Graph, Node, NodeType
from kubedian.i18n import t

_TAG = {
    NodeType.SERVICE: "service",
    NodeType.DATABASE: "database",
    NodeType.CACHE: "cache",
    NodeType.QUEUE: "queue",
    NodeType.EXTERNAL_API: "external",
    NodeType.HELM_CHART: "chart",
    NodeType.NAMESPACE: "namespace",
    NodeType.GATEWAY: "gateway",
    NodeType.STORAGE: "storage",
    NodeType.AUTOSCALER: "autoscaler",
    NodeType.SERVICE_ACCOUNT: "serviceaccount",
    NodeType.JOB: "job",
    NodeType.CRONJOB: "cronjob",
}

_EDGE_FIELD = {
    EdgeType.HTTP_CALLS: "Calls",
    EdgeType.READS_FROM: "Reads from",
    EdgeType.WRITES_TO: "Writes to",
    EdgeType.CACHES_IN: "Caches in",
    EdgeType.QUEUES_TO: "Queues to",
    EdgeType.AUTHENTICATES_VIA: "Authenticates via",
    EdgeType.CALLS_EXTERNAL: "Calls external",
    EdgeType.DEPENDS_ON_CHART: "Depends on chart",
    EdgeType.ROUTES_TO: "Routes to",
    EdgeType.ALLOWS_TO: "Allowed to reach",
    EdgeType.SELECTS: "Selects",
    EdgeType.MOUNTS: "Mounts",
    EdgeType.SCALES: "Scales",
    EdgeType.RUNS_AS: "Runs as",
    EdgeType.OWNS: "Owns",
    EdgeType.REFERENCES: "Mounts config",
}


def export_vault(graph: Graph, out_dir: Path, *, lang: str = "en") -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    callees: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if e.type == EdgeType.IN_NAMESPACE:
            continue
        callees.setdefault(e.src_id, []).append(e)

    count = 0
    for node in graph.nodes.values():
        text = _note(node, graph, callees.get(node.id, []), lang)
        assert_no_secret_values(text)
        (out_dir / f"{_filename(node)}.md").write_text(text, encoding="utf-8")
        count += 1

    overview = _overview(graph, lang)
    assert_no_secret_values(overview)
    (out_dir / "_Overview.md").write_text(overview, encoding="utf-8")
    return count


def _note(node: Node, graph: Graph, out_edges: list[Edge], lang: str) -> str:
    tag = _TAG.get(node.type, node.type.value)
    lines = [
        "---",
        f"type: {node.type.value}",
        f"namespace: {node.namespace or ''}",
        f"tags: [{tag}]",
    ]
    if node.attrs.get("image"):
        lines.append(f"image: {node.attrs['image']}")
    lines += ["---", "", f"# {node.name}", ""]

    if out_edges:
        for e in out_edges:
            field = _EDGE_FIELD.get(e.type, e.type.value)
            target = graph.nodes.get(e.dst_id)
            target_name = _filename(target) if target else e.dst_id
            prov = (
                t(lang, "docs", "provenance_explicit")
                if e.provenance.value == "explicit"
                else t(lang, "docs", "provenance_heuristic")
            )
            cite = f" <small>({prov}"
            if e.source_locator:
                cite += f": `{e.source_locator}`"
            cite += ")</small>"
            lines.append(f"- **{field}:: [[{target_name}]]**{cite}")
        lines.append("")

    if node.type == NodeType.SERVICE:
        lines += [
            f"## {t(lang, 'docs', 'diagram')}",
            "",
            "```mermaid",
            mermaid_renderer.render_focus(graph, node.id, lang=lang).rstrip(),
            "```",
            "",
        ]
    return "\n".join(lines)


def _overview(graph: Graph, lang: str) -> str:
    return "\n".join(
        [
            "---",
            "tags: [overview]",
            "---",
            "",
            f"# {t(lang, 'docs', 'title')}",
            "",
            "```mermaid",
            mermaid_renderer.render_flowchart(graph, lang=lang).rstrip(),
            "```",
            "",
        ]
    )


def _filename(node: Node) -> str:
    # Use the node name; disambiguate datastores by namespace-less family id.
    base = node.name
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in base)
