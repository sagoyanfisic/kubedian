"""Stage 0-4 orchestrator: discover -> extract -> resolve -> store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import ExtractResult, extract_overlay
from kubedian.application.pipeline.resolve import resolve
from kubedian.domain.entities.graph import Environment, Graph
from kubedian.domain.entities.resource import RenderMode
from kubedian.infrastructure.kustomize.runner import kustomize_version
from kubedian.infrastructure.sqlite import graph_store


@dataclass
class IndexReport:
    repo: Path
    db_path: Path
    overlays_total: int = 0
    render_failures: int = 0
    failed_overlays: list[str] = field(default_factory=list)
    documented_edges: int = 0
    graph: Graph | None = None


# (done, total, label) -> None, called after each overlay is processed.
ProgressHook = Callable[[int, int, str], None]


def index_repo(
    repo: Path,
    db_path: Path,
    environment: Environment | None = None,
    on_overlay: Optional[ProgressHook] = None,
    docs_dir: Optional[Path] = None,
) -> IndexReport:
    repo = repo.resolve()
    overlays = discover_overlays(repo, environment)
    report = IndexReport(repo=repo, db_path=db_path, overlays_total=len(overlays))

    results: list[ExtractResult] = []
    for i, overlay in enumerate(overlays, start=1):
        result = extract_overlay(overlay)
        results.append(result)
        if result.render_mode == RenderMode.RAW_FALLBACK:
            report.render_failures += 1
            report.failed_overlays.append(f"{overlay.service}/{overlay.environment.value}")
        if on_overlay is not None:
            on_overlay(i, len(overlays), overlay.service)

    graph = resolve(results)

    if docs_dir is not None:
        from kubedian.application.pipeline.docs_ingest import ingest_docs
        from kubedian.application.pipeline.resolve import _dedup_edges

        envs = [environment] if environment else list(Environment)
        report.documented_edges = ingest_docs(graph, docs_dir, envs)
        # Re-dedup so a documented edge merges with an existing explicit/heuristic
        # one for the same (src, dst, type, env) — explicit/higher-confidence wins.
        graph.edges = _dedup_edges(graph.edges)

    report.graph = graph

    graph_store.write_graph(
        db_path,
        graph,
        repo_path=str(repo),
        indexed_at=datetime.now(timezone.utc).isoformat(),
        kustomize_version=kustomize_version(),
        render_failures=report.render_failures,
    )
    return report
