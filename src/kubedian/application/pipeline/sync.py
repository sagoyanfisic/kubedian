"""`sync-envs`: refresh only the secret/env subset of the graph (names + file
locations, never values) using a mark-and-sweep generation bump.

Why a separate command from ``index``: a full index drops and rebuilds the whole
DB (and re-renders every overlay). ``sync-envs`` re-reads the manifests but only
touches the secret/configmap nodes and the env-derived edges, leaving services,
jobs and real call edges untouched — and sweeps the relations that disappeared.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.application.pipeline.resolve import resolve
from kubedian.domain.entities.graph import Environment, NodeType, Signal
from kubedian.infrastructure.sqlite import graph_store

# Edges that are derived from env vars / mounted secrets & configmaps. These are the
# only relations sync-envs owns and is allowed to sweep. (DNS_LITERAL is deliberately
# excluded: it also carries genuine service→service hops, so a full `index` owns it.)
ENV_EDGE_SIGNALS: tuple[str, ...] = (
    Signal.SECRET_KEY_NAME.value,
    Signal.VOLUME_MOUNT.value,
    Signal.ENV_FROM.value,
    Signal.ENV_KEY_REF.value,
    Signal.ENV_LITERAL.value,
    Signal.CONFIGMAP_URL.value,
)

# Nodes sync-envs fully owns and may delete when they vanish from the manifests.
SWEEP_NODE_TYPES: tuple[str, ...] = (NodeType.SECRET.value, NodeType.CONFIGMAP.value)

ProgressHook = Callable[[int, int, str], None]


def sync_envs(
    repo: Path,
    db_path: Path,
    environment: Environment | None = None,
    on_overlay: Optional[ProgressHook] = None,
) -> dict:
    """Re-read the repo and mark-and-sweep the secret/env subset of the graph."""
    repo = repo.resolve()
    overlays = discover_overlays(repo, environment)
    results = []
    for i, overlay in enumerate(overlays, start=1):
        results.append(extract_overlay(overlay))
        if on_overlay is not None:
            on_overlay(i, len(overlays), overlay.service)

    graph = resolve(results)

    # Keep only env-derived edges, then the nodes they touch plus every secret/configmap
    # (a volume-only secret may have no datastore edge but must still be (re)written).
    edges = [e for e in graph.edges if e.signal.value in ENV_EDGE_SIGNALS]
    endpoints = {e.src_id for e in edges} | {e.dst_id for e in edges}
    nodes = [
        n
        for n in graph.nodes.values()
        if n.id in endpoints or n.type.value in SWEEP_NODE_TYPES
    ]

    return graph_store.sync_env_subset(
        db_path,
        nodes,
        edges,
        sweep_node_types=SWEEP_NODE_TYPES,
        sweep_edge_signals=ENV_EDGE_SIGNALS,
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )
