from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.application.pipeline.resolve import resolve
from kubedian.domain.entities.graph import Environment
from kubedian.infrastructure.mermaid import mermaid_renderer
from tests.conftest import write_sample_repo


def _graph(tmp_path):
    repo = write_sample_repo(tmp_path)
    overlays = discover_overlays(repo, Environment.STAGING)
    return resolve([extract_overlay(o) for o in overlays])


def test_flowchart_is_well_formed(tmp_path):
    out = mermaid_renderer.render_flowchart(_graph(tmp_path), lang="en")
    assert out.startswith("flowchart LR")
    assert "classDef service" in out
    # balanced subgraph/end
    assert out.count("subgraph ") == out.count("\n    end")
    # explicit edges are solid, heuristic edges dotted
    assert "-->" in out
    assert "-.->" in out  # heuristic db/queue edges


def test_focus_only_includes_neighbours(tmp_path):
    out = mermaid_renderer.render_focus(_graph(tmp_path), "svc:ns-a/service-a", lang="en")
    assert "service-a" in out
    assert "service-b" in out


def test_labels_are_translated(tmp_path):
    g = _graph(tmp_path)
    en = mermaid_renderer.render_focus(g, "svc:ns-a/service-a", lang="en")
    es = mermaid_renderer.render_focus(g, "svc:ns-a/service-a", lang="es")
    assert "calls" in en
    assert "llama a" in es


def test_no_inline_namespace_edges(tmp_path):
    out = mermaid_renderer.render_flowchart(_graph(tmp_path), lang="en")
    assert "in namespace" not in out
