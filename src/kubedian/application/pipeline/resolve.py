"""Stage 3 — turn extracted resources into a graph of nodes and edges.

Two passes:

1. **Index pass** — register every service and its aliases (k8s Service names,
   workload names, app labels) and build a global registry of "service-discovery"
   ConfigMaps that map ``*_API_URL`` keys to in-cluster service targets.
2. **Edge pass** — for each service, emit edges from: configmap URLs it consumes
   (explicit), literal cluster-DNS env values (explicit), secret key-name
   heuristics (heuristic), and helmCharts (explicit).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from kubedian.application.heuristics import env_key_rules
from kubedian.application.heuristics.dns import ClusterTarget, is_cluster_internal, parse_cluster_host
from kubedian.application.pipeline.extract import ExtractResult
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
from kubedian.domain.entities.resource import DeploymentView, K8sResource


@dataclass
class _ServiceIndex:
    # alias (namespace, name) -> service node id
    by_alias: dict[tuple[str, str], str] = field(default_factory=dict)
    # configmap name -> {env_key: ClusterTarget}
    discovery_configmaps: dict[str, dict[str, ClusterTarget]] = field(default_factory=dict)


def resolve(results: list[ExtractResult]) -> Graph:
    graph = Graph()
    index = _ServiceIndex()

    # ---- Pass 1: register services + discovery configmaps -------------------
    service_for_result: dict[int, str] = {}
    for i, result in enumerate(results):
        ns = _service_namespace(result)
        svc_name = result.overlay.service
        node_id = f"svc:{ns}/{svc_name}"
        graph.add_node(
            Node(
                id=node_id,
                type=NodeType.SERVICE,
                name=svc_name,
                namespace=ns,
                attrs={"environment": result.overlay.environment.value, **_service_attrs(result)},
            )
        )
        service_for_result[i] = node_id
        _register_aliases(index, result, ns, node_id)
        _collect_discovery_configmaps(index, result)
        # namespace node + membership edge
        graph.add_node(Node(id=f"ns:{ns}", type=NodeType.NAMESPACE, name=ns))
        graph.add_edge(
            Edge(
                src_id=node_id,
                dst_id=f"ns:{ns}",
                type=EdgeType.IN_NAMESPACE,
                environment=result.overlay.environment,
                provenance=Provenance.EXPLICIT,
                signal=Signal.NAMESPACE,
                source_file=result.overlay.kustomization.as_posix(),
            )
        )

    # ---- Pass 2: edges ------------------------------------------------------
    for i, result in enumerate(results):
        node_id = service_for_result[i]
        env = result.overlay.environment
        _resolve_service_edges(graph, index, result, node_id, env)

    graph.edges = _dedup_edges(graph.edges)
    return graph


def _dedup_edges(edges: list[Edge]) -> list[Edge]:
    merged: dict[tuple, Edge] = {}
    for edge in edges:
        key = edge.key
        existing = merged.get(key)
        if existing is None:
            if edge.source_locator:
                edge.attrs.setdefault("locators", [edge.source_locator])
            merged[key] = edge
            continue
        # merge into existing
        locators = existing.attrs.setdefault("locators", [])
        if existing.source_locator and existing.source_locator not in locators:
            locators.append(existing.source_locator)
        if edge.source_locator and edge.source_locator not in locators:
            locators.append(edge.source_locator)
        # prefer explicit provenance / higher confidence
        if (existing.provenance != Provenance.EXPLICIT and edge.provenance == Provenance.EXPLICIT) or (
            edge.confidence > existing.confidence
        ):
            edge.attrs["locators"] = locators
            merged[key] = edge
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Pass 1 helpers
# --------------------------------------------------------------------------- #
def _service_namespace(result: ExtractResult) -> str:
    """Authoritative namespace for the service.

    The overlay's declared ``namespace:`` is what kustomize's namespace
    transformer applies to every resource, so it wins. Otherwise fall back to
    the most common namespace among rendered workloads/services.
    """
    if result.overlay_namespace:
        return result.overlay_namespace
    counter: Counter[str] = Counter()
    for res in result.resources:
        if res.namespace and res.kind in ("Deployment", "StatefulSet", "Service", "Job"):
            counter[res.namespace] += 1
    if counter:
        return counter.most_common(1)[0][0]
    # fall back to any namespaced resource, then "default"
    for res in result.resources:
        if res.namespace:
            return res.namespace
    return "default"


def _service_attrs(result: ExtractResult) -> dict:
    image = None
    for dep in result.deployments:
        for c in dep.containers:
            if c.image:
                image = c.image
                break
        if image:
            break
    return {
        "render_mode": result.render_mode.value,
        "image": image,
        "render_error": result.render_error,
    }


def _register_aliases(index: _ServiceIndex, result: ExtractResult, ns: str, node_id: str) -> None:
    index.by_alias[(ns, result.overlay.service)] = node_id
    for res in result.resources:
        rns = res.namespace or ns
        if res.kind == "Service" and res.name:
            index.by_alias[(rns, res.name)] = node_id
        if res.kind in ("Deployment", "StatefulSet") and res.name:
            index.by_alias[(rns, res.name)] = node_id
    for dep in result.deployments:
        if dep.app_label:
            index.by_alias[(ns, dep.app_label)] = node_id


def _collect_discovery_configmaps(index: _ServiceIndex, result: ExtractResult) -> None:
    for res in result.resources:
        if res.kind != "ConfigMap":
            continue
        data = res.raw.get("data") or {}
        targets: dict[str, ClusterTarget] = {}
        for key, value in data.items():
            if not isinstance(value, str):
                continue
            target = parse_cluster_host(value)
            if target is not None:
                targets[str(key)] = target
        if targets:
            index.discovery_configmaps[res.name] = targets


# --------------------------------------------------------------------------- #
# Pass 2 helpers
# --------------------------------------------------------------------------- #
def _resolve_service_edges(
    graph: Graph,
    index: _ServiceIndex,
    result: ExtractResult,
    node_id: str,
    env: Environment,
) -> None:
    secret_index = {s.name: s for s in result.secrets}

    for dep in result.deployments:
        for container in dep.containers:
            # 1. configmaps consumed via envFrom -> explicit http_calls
            for cm_name in container.env_from_configmaps:
                targets = index.discovery_configmaps.get(cm_name)
                if not targets:
                    continue
                for env_key, target in targets.items():
                    dst = _ensure_service_target(graph, index, target)
                    if dst == node_id:
                        continue
                    graph.add_edge(
                        Edge(
                            src_id=node_id,
                            dst_id=dst,
                            type=EdgeType.HTTP_CALLS,
                            environment=env,
                            provenance=Provenance.EXPLICIT,
                            signal=Signal.CONFIGMAP_URL,
                            source_file=dep.resource.source_file,
                            source_locator=f"{cm_name}.{env_key}",
                        )
                    )

            # 2. literal env values -> explicit edges
            for key, value in container.env.items():
                if parse_cluster_host(value) is not None:
                    target = parse_cluster_host(value)
                    dst = _ensure_service_target(graph, index, target)  # type: ignore[arg-type]
                    if dst != node_id:
                        graph.add_edge(
                            Edge(
                                src_id=node_id,
                                dst_id=dst,
                                type=EdgeType.HTTP_CALLS,
                                environment=env,
                                provenance=Provenance.EXPLICIT,
                                signal=Signal.DNS_LITERAL,
                                source_file=dep.resource.source_file,
                                source_locator=key,
                            )
                        )
                elif "://" in value and not is_cluster_internal(value):
                    _emit_external(graph, node_id, env, key, value, dep.resource.source_file, Signal.ENV_LITERAL)

            # 3. secret key-name heuristics
            for secret_name in container.env_from_secrets:
                secret = secret_index.get(secret_name)
                if secret is None:
                    continue
                for key in secret.key_names:
                    _emit_heuristic(graph, node_id, result.overlay.service, env, key, secret.source_file)

        # 4. volume-mounted config (per deployment, not per container)
        ns = graph.nodes[node_id].namespace or "default"
        for cm_name in dep.configmap_volumes:
            targets = index.discovery_configmaps.get(cm_name)
            if targets:  # a service-discovery configmap mounted as a file
                for env_key, target in targets.items():
                    dst = _ensure_service_target(graph, index, target)
                    if dst == node_id:
                        continue
                    graph.add_edge(Edge(
                        src_id=node_id, dst_id=dst, type=EdgeType.HTTP_CALLS, environment=env,
                        provenance=Provenance.EXPLICIT, signal=Signal.CONFIGMAP_URL,
                        source_file=dep.resource.source_file, source_locator=f"{cm_name}.{env_key}",
                    ))
            else:  # plain config — record the dependency on the configmap
                _emit_reference(graph, node_id, NodeType.CONFIGMAP, "cm", ns, cm_name, env, dep.resource.source_file)
        for secret_name in dep.secret_volumes:
            _emit_reference(graph, node_id, NodeType.SECRET, "secret", ns, secret_name, env, dep.resource.source_file)

    # 5. helmCharts -> explicit depends_on_chart
    for chart in result.helm_charts:
        chart_id = f"chart:{(chart.repo or '')}/{chart.name}@{chart.version or 'latest'}"
        graph.add_node(
            Node(
                id=chart_id,
                type=NodeType.HELM_CHART,
                name=chart.name,
                attrs={"repo": chart.repo, "version": chart.version},
            )
        )
        graph.add_edge(
            Edge(
                src_id=node_id,
                dst_id=chart_id,
                type=EdgeType.DEPENDS_ON_CHART,
                environment=env,
                provenance=Provenance.EXPLICIT,
                signal=Signal.HELMCHART,
                source_file=chart.source_file,
            )
        )


def _ensure_service_target(graph: Graph, index: _ServiceIndex, target: ClusterTarget) -> str:
    node_id = index.by_alias.get((target.namespace, target.service))
    if node_id is not None:
        return node_id
    # external/unindexed in-cluster service: create a node so the edge resolves
    node_id = f"svc:{target.namespace}/{target.service}"
    graph.add_node(
        Node(
            id=node_id,
            type=NodeType.SERVICE,
            name=target.service,
            namespace=target.namespace,
            attrs={"discovered": True},
        )
    )
    index.by_alias[(target.namespace, target.service)] = node_id
    return node_id


def _emit_heuristic(
    graph: Graph,
    src_id: str,
    service_name: str,
    env: Environment,
    key: str,
    source_file: str,
) -> None:
    hint = env_key_rules.hint_for_key(key)
    if hint is None:
        return
    if hint.target_node_type == NodeType.EXTERNAL_API:
        dst_id = f"ext:{hint.family}"
        name = hint.family
    elif hint.target_node_type == NodeType.SERVICE:
        dst_id = f"svc-logical:{hint.family}"
        name = hint.family
    else:
        dst_id = f"{hint.target_node_type.value}:{service_name}/{hint.family}"
        name = f"{hint.family} ({service_name})"
    graph.add_node(
        Node(id=dst_id, type=hint.target_node_type, name=name, attrs={"family": hint.family})
    )
    graph.add_edge(
        Edge(
            src_id=src_id,
            dst_id=dst_id,
            type=hint.edge_type,
            environment=env,
            provenance=Provenance.HEURISTIC,
            signal=Signal.SECRET_KEY_NAME,
            source_file=source_file,
            source_locator=key,
            confidence=hint.confidence,
        )
    )


def _emit_reference(
    graph: Graph,
    src_id: str,
    node_type: NodeType,
    prefix: str,
    ns: str,
    name: str,
    env: Environment,
    source_file: str,
) -> None:
    """Record that a service mounts a ConfigMap/Secret as config (no values read)."""
    dst_id = f"{prefix}:{ns}/{name}"
    graph.add_node(Node(id=dst_id, type=node_type, name=name, namespace=ns))
    graph.add_edge(
        Edge(
            src_id=src_id,
            dst_id=dst_id,
            type=EdgeType.REFERENCES,
            environment=env,
            provenance=Provenance.EXPLICIT,
            signal=Signal.VOLUME_MOUNT,
            source_file=source_file,
        )
    )


def _emit_external(
    graph: Graph,
    src_id: str,
    env: Environment,
    key: str,
    value: str,
    source_file: str,
    signal: Signal,
) -> None:
    from kubedian.application.heuristics.dns import _extract_host

    host = _extract_host(value) or value
    slug = host.replace(".", "-")
    dst_id = f"ext:{slug}"
    graph.add_node(Node(id=dst_id, type=NodeType.EXTERNAL_API, name=host, attrs={"host": host}))
    graph.add_edge(
        Edge(
            src_id=src_id,
            dst_id=dst_id,
            type=EdgeType.CALLS_EXTERNAL,
            environment=env,
            provenance=Provenance.EXPLICIT,
            signal=signal,
            source_file=source_file,
            source_locator=key,
        )
    )
