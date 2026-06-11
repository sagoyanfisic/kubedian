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
from kubedian.application.heuristics.dns import (
    ClusterTarget,
    extract_host,
    is_cluster_internal,
    parse_cluster_host,
)
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
from kubedian.domain.entities.resource import DeploymentView, EnvKeyRef, K8sResource

# Kinds the resolver turns into graph nodes/edges/attrs. Anything else is counted
# into index_meta.ignored_kinds so nothing is *silently* dropped.
HANDLED_KINDS: frozenset[str] = frozenset({
    "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob",
    "Service", "Ingress", "VirtualService", "Gateway", "NetworkPolicy",
    "PersistentVolumeClaim", "HorizontalPodAutoscaler", "ServiceAccount",
    "ConfigMap", "Secret", "Namespace",
    "PodDisruptionBudget", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding",
    "ScaledObject",
})


@dataclass
class _ServiceIndex:
    # alias (namespace, name) -> service node id
    by_alias: dict[tuple[str, str], str] = field(default_factory=dict)
    # namespace -> [(pod_labels, service node id)] for generic selector matching
    by_labels: dict[str, list[tuple[dict[str, str], str]]] = field(default_factory=dict)
    # configmap name -> {env_key: ClusterTarget}
    discovery_configmaps: dict[str, dict[str, ClusterTarget]] = field(default_factory=dict)
    # names of discovery configmaps shared across services (a catalog, not a call)
    catalog_configmaps: set[str] = field(default_factory=set)


def resolve(results: list[ExtractResult]) -> Graph:
    graph = Graph()
    index = _ServiceIndex()

    # Each distinct workload (Deployment/StatefulSet/...) is its own service node;
    # a directory bundling api + celery worker/beat/flower yields several nodes, so
    # a sub-workload's PVC/HPA/ingress attach to the right node, not the api.
    pairs_for_result: dict[int, list[tuple[DeploymentView, str]]] = {}
    primary_for_result: dict[int, str] = {}
    ns_for_result: dict[int, str] = {}

    # ---- Pass 1A: one node per workload -------------------------------------
    for i, result in enumerate(results):
        ns = _service_namespace(result)
        ns_for_result[i] = ns
        env = result.overlay.environment
        pairs: list[tuple[DeploymentView, str]] = []
        for dep in result.deployments:
            wname = dep.resource.name or result.overlay.service
            node_id = f"svc:{ns}/{wname}"
            graph.add_node(Node(
                id=node_id, type=_node_type_for(dep.workload_kind), name=wname, namespace=ns,
                attrs={"environment": env.value, **_workload_attrs(result, dep)},
            ))
            index.by_alias[(ns, wname)] = node_id
            # The workload *name* is authoritative (Services, HPA scaleTargetRef and
            # Ingress backends reference workloads by name). An `app` label is often
            # SHARED across a bundle's workloads (api/worker/beat/flower), so it must
            # only fill a gap, never overwrite a name alias.
            if dep.app_label:
                index.by_alias.setdefault((ns, dep.app_label), node_id)
            if dep.pod_labels:
                index.by_labels.setdefault(ns, []).append((dep.pod_labels, node_id))
            pairs.append((dep, node_id))
        pairs_for_result[i] = pairs
        _collect_discovery_configmaps(index, result)

    # ---- Pass 1B: services (alias to the workload they front), namespaces ----
    for i, result in enumerate(results):
        ns = ns_for_result[i]
        env = result.overlay.environment
        pairs = pairs_for_result[i]
        service_nodes = _register_services(graph, index, result, ns)
        if pairs:
            # prefer a long-running workload as the overlay's representative, so
            # helmCharts / VS-owner edges never hang off a one-off Job/CronJob.
            primary = next(
                (nid for d, nid in pairs if d.workload_kind in _SERVICE_KINDS),
                pairs[0][1],
            )
        elif service_nodes:
            primary = service_nodes[0]
        else:  # an overlay with neither workload nor Service — keep one node
            primary = f"svc:{ns}/{result.overlay.service}"
            graph.add_node(Node(id=primary, type=NodeType.SERVICE, name=result.overlay.service,
                               namespace=ns, attrs={"environment": env.value, **_overlay_attrs(result)}))
        primary_for_result[i] = primary
        index.by_alias.setdefault((ns, result.overlay.service), primary)
        graph.add_node(Node(id=f"ns:{ns}", type=NodeType.NAMESPACE, name=ns))
        for nid in {p[1] for p in pairs} | set(service_nodes) | {primary}:
            if graph.nodes[nid].namespace != ns:
                continue
            graph.add_edge(Edge(
                src_id=nid, dst_id=f"ns:{ns}", type=EdgeType.IN_NAMESPACE, environment=env,
                provenance=Provenance.EXPLICIT, signal=Signal.NAMESPACE,
                source_file=result.overlay.kustomization.as_posix(),
            ))

    # ---- Detect shared service-discovery catalogs ---------------------------
    # A discovery ConfigMap consumed by several services that lists many targets
    # is a *catalog*: a service having a target's URL available does not prove it
    # calls that target, so edges derived from it must be heuristic, not facts.
    consumers: dict[str, set[str]] = {}
    for i, result in enumerate(results):
        for dep, node_id in pairs_for_result[i]:
            names = set(dep.configmap_volumes)
            for c in dep.containers:
                names.update(c.env_from_configmaps)
            for cm in names:
                if cm in index.discovery_configmaps:
                    consumers.setdefault(cm, set()).add(node_id)
    index.catalog_configmaps = {
        cm
        for cm, svcs in consumers.items()
        if len(svcs) >= 2 and len(index.discovery_configmaps.get(cm, {})) >= 3
    }

    # ---- Pass 2: edges ------------------------------------------------------
    for i, result in enumerate(results):
        env = result.overlay.environment
        pairs = pairs_for_result[i]
        primary = primary_for_result[i]
        _resolve_service_edges(graph, index, result, pairs, primary, env)
        _resolve_routing_edges(graph, index, result, primary, env)
        _resolve_network_policy_edges(graph, index, result, primary, env)
        _resolve_storage_edges(graph, index, result, pairs, primary, env)
        _resolve_autoscaler_edges(graph, index, result, primary, env)
        _resolve_identity_edges(graph, index, result, pairs, primary, env)
        _resolve_rbac_edges(graph, index, result, primary, env)
        _resolve_pdb_attrs(graph, index, result, primary)
        _resolve_gateway_edges(graph, index, result, primary, env)
        _resolve_owner_edges(graph, index, result, primary, env)

    graph.edges = _dedup_edges(graph.edges)
    return graph


def _register_services(graph: Graph, index: _ServiceIndex, result: ExtractResult, ns: str) -> list[str]:
    """A Service is the front of the workload it selects. Alias its name to that
    workload node (usually same-named); if it fronts nothing indexed, give it its
    own node (e.g. a Service with no in-repo workload)."""
    nodes: list[str] = []
    for res in result.resources:
        if res.kind != "Service" or not res.name:
            continue
        rns = res.namespace or ns
        spec = res.raw.get("spec") or {}
        if spec.get("type") == "ExternalName" and spec.get("externalName"):
            # An ExternalName Service is a DNS alias to an outside host: consumers
            # resolving this name are really calling the external API, so alias the
            # Service name to an external node. setdefault: never shadow a workload.
            ext_id = _ensure_external_node(graph, str(spec["externalName"]))
            graph.nodes[ext_id].attrs.setdefault("external_name_service", res.name)
            index.by_alias.setdefault((rns, res.name), ext_id)
            index.by_alias.setdefault((ns, res.name), ext_id)
            continue
        target = index.by_alias.get((rns, res.name))
        if target is None:
            sel = (res.raw.get("spec") or {}).get("selector") or {}
            target = _match_selector(index, rns, {str(k): str(v) for k, v in sel.items()})
        if target is None:
            target = f"svc:{rns}/{res.name}"
            graph.add_node(Node(id=target, type=NodeType.SERVICE, name=res.name, namespace=rns))
        index.by_alias[(rns, res.name)] = target
        index.by_alias.setdefault((ns, res.name), target)
        # Many workloads omit containerPort; the Service declares the real port.
        node = graph.nodes.get(target)
        if node is not None:
            if not node.attrs.get("ports"):
                sp = _service_ports(res.raw)
                if sp:
                    node.attrs["ports"] = sp
            # Several Services may front the same workload (e.g. an edge proxy
            # selecting another service's pods) — keep every distinct wiring,
            # tagged with the Service that declares it.
            pm = _service_port_map(res.raw, node.attrs.get("named_ports"))
            if pm:
                existing = node.attrs.setdefault("port_map", [])
                for entry in pm:
                    entry["service"] = res.name
                    if entry not in existing:
                        existing.append(entry)
        nodes.append(target)
    return nodes


def _service_ports(raw: dict) -> list[int]:
    ports: list[int] = []
    for p in (raw.get("spec") or {}).get("ports") or []:
        if not isinstance(p, dict):
            continue
        val = p.get("targetPort")
        if not isinstance(val, int):  # targetPort may be a named string port
            val = p.get("port")
        if isinstance(val, int):
            ports.append(val)
    return list(dict.fromkeys(ports))


def _service_port_map(raw: dict, named_ports: dict | None) -> list[dict]:
    """The Service's declared port wiring, one entry per spec.ports[] item.

    ``target_port`` is the resolved container port: a named targetPort is looked
    up in the workload's named containerPorts; an absent targetPort defaults to
    ``port`` (Kubernetes' own default).
    """
    entries: list[dict] = []
    for p in (raw.get("spec") or {}).get("ports") or []:
        if not isinstance(p, dict) or not isinstance(p.get("port"), int):
            continue
        target = p.get("targetPort", p["port"])
        if isinstance(target, str):
            target = (named_ports or {}).get(target, target)
        entries.append({
            "name": p.get("name"),
            "port": p["port"],
            "target_port": target,
            "protocol": p.get("protocol") or "TCP",
        })
    return entries


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
        if edge.type == EdgeType.REFERENCES:
            # The same Secret/ConfigMap may be consumed through several modes
            # (envFrom + valueFrom + volume). Edge.key collapses them into one
            # edge, so union the consumption facts instead of dropping them.
            _merge_reference_attrs(existing.attrs, edge.attrs)
        # prefer explicit provenance / higher confidence
        if (existing.provenance != Provenance.EXPLICIT and edge.provenance == Provenance.EXPLICIT) or (
            edge.confidence > existing.confidence
        ):
            edge.attrs["locators"] = locators
            if edge.type == EdgeType.REFERENCES:
                _merge_reference_attrs(edge.attrs, existing.attrs)
            merged[key] = edge
    return list(merged.values())


def _merge_reference_attrs(target: dict, other: dict) -> None:
    """Union ``keys``/``modes``/``mount_paths`` and merge ``env_map`` of two REFERENCES edges."""
    for field_name in ("keys", "modes", "mount_paths"):
        values = list(target.get(field_name) or []) + list(other.get(field_name) or [])
        if values:
            target[field_name] = list(dict.fromkeys(values))
    env_map = {**(other.get("env_map") or {}), **(target.get("env_map") or {})}
    if env_map:
        target["env_map"] = env_map


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


_SERVICE_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}


def _node_type_for(workload_kind: str) -> NodeType:
    """Long-running workloads are services; batch workloads get their own type so
    migrations/backups don't pollute service queries, diagrams or blast-radius."""
    if workload_kind == "Job":
        return NodeType.JOB
    if workload_kind == "CronJob":
        return NodeType.CRONJOB
    return NodeType.SERVICE


def _workload_attrs(result: ExtractResult, dep: DeploymentView) -> dict:
    """Per-workload facts kept on its service node."""
    image = next((c.image for c in dep.containers if c.image), None)
    # Env var NAMES only (literals + valueFrom targets) — values are never stored.
    env_vars: set[str] = set()
    for c in dep.containers:
        env_vars.update(c.env.keys())
        env_vars.update(r.var for r in c.secret_key_refs)
        env_vars.update(r.var for r in c.configmap_key_refs)
    return {
        "render_mode": result.render_mode.value,
        "image": image,
        "render_error": result.render_error,
        "workload_kind": dep.workload_kind,
        "replicas": dep.replicas,
        "ports": list(dep.ports),
        "named_ports": dict(dep.named_ports) or None,
        "env_vars": sorted(env_vars),
        "service_account": dep.service_account,
        "nodepool": dep.nodepool,
        # Bundle membership: workloads sharing the overlay are siblings (api/worker/beat).
        "overlay": result.overlay.service,
        "app_label": dep.app_label,
        "containers": _container_attrs(dep) or None,
    }


def _container_attrs(dep: DeploymentView) -> list[dict]:
    """Compact per-container facts (role, resources, probes, mount paths).

    Empty/None fields are omitted to keep the node's JSON attrs small — this
    detail is only serialized in full by the composition query, not node_dict.
    """
    out: list[dict] = []
    for c in dep.containers:
        entry: dict = {"name": c.name, "role": c.role}
        if c.image:
            entry["image"] = c.image
        if c.resources:
            entry["resources"] = c.resources
        if c.probes:
            entry["probes"] = c.probes
        if c.volume_mounts:
            entry["mounts"] = [path for _, path in c.volume_mounts]
        out.append(entry)
    return out


def _overlay_attrs(result: ExtractResult) -> dict:
    """Attrs for the fallback node of an overlay that declares no workload."""
    return {"render_mode": result.render_mode.value, "render_error": result.render_error}


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
def _configmap_key_index(result: ExtractResult) -> dict[str, list[str]]:
    """ConfigMap name -> its data key names (names only, values are never kept)."""
    out: dict[str, list[str]] = {}
    for res in result.resources:
        if res.kind != "ConfigMap" or not res.name:
            continue
        keys = list((res.raw.get("data") or {}).keys()) + list((res.raw.get("binaryData") or {}).keys())
        if keys:
            out[res.name] = [str(k) for k in keys]
    return out


def _group_key_refs(refs: tuple[EnvKeyRef, ...]) -> dict[str, list[EnvKeyRef]]:
    """Group a container's valueFrom refs by referenced Secret/ConfigMap name."""
    grouped: dict[str, list[EnvKeyRef]] = {}
    for r in refs:
        grouped.setdefault(r.ref, []).append(r)
    return grouped


def _resolve_service_edges(
    graph: Graph,
    index: _ServiceIndex,
    result: ExtractResult,
    pairs: list[tuple[DeploymentView, str]],
    primary: str,
    env: Environment,
) -> None:
    """Config/env-derived edges — one helper per consumption mode."""
    secret_index = {s.name: s for s in result.secrets}
    configmap_index = _configmap_key_index(result)
    service_name = result.overlay.service

    for dep, node_id in pairs:
        dep_ns = graph.nodes[node_id].namespace or "default"
        for container in dep.containers:
            _envfrom_configmap_edges(graph, index, node_id, dep_ns, container, env,
                                     dep.resource.source_file, configmap_index)
            _envkeyref_edges(graph, index, node_id, dep_ns, container, env,
                             dep.resource.source_file, secret_index, service_name)
            _env_literal_edges(graph, index, node_id, container, env, dep.resource.source_file)
            _envfrom_secret_edges(graph, node_id, dep_ns, container, env,
                                  secret_index, service_name)
        _volume_config_edges(graph, index, dep, node_id, env, secret_index)

    _helmchart_edges(graph, result, primary, env)


def _envfrom_configmap_edges(
    graph: Graph,
    index: _ServiceIndex,
    node_id: str,
    dep_ns: str,
    container,
    env: Environment,
    source_file: str,
    configmap_index: dict[str, list[str]],
) -> None:
    """ConfigMaps consumed via envFrom -> http_calls (heuristic if the configmap
    is a shared service-discovery catalog). Either way the workload also
    REFERENCES the configmap itself (key names only)."""
    for cm_name in container.env_from_configmaps:
        _emit_reference(
            graph, node_id, NodeType.CONFIGMAP, "cm", dep_ns, cm_name, env,
            source_file, signal=Signal.ENV_FROM,
            keys=configmap_index.get(cm_name),
        )
        targets = index.discovery_configmaps.get(cm_name)
        if not targets:
            continue
        catalog = cm_name in index.catalog_configmaps
        for env_key, target in targets.items():
            dst = _ensure_service_target(graph, index, target)
            if dst == node_id:
                continue
            _emit_configmap_call(
                graph, node_id, dst, env, cm_name, env_key, source_file, catalog=catalog,
            )


def _envkeyref_edges(
    graph: Graph,
    index: _ServiceIndex,
    node_id: str,
    dep_ns: str,
    container,
    env: Environment,
    source_file: str,
    secret_index: dict,
    service_name: str,
) -> None:
    """Single keys wired via env[].valueFrom.{secret,configMap}KeyRef: one
    REFERENCES edge per referenced object carrying var->key names (never values),
    plus the same key-name heuristics as envFrom."""
    for ref_name, refs in _group_key_refs(container.secret_key_refs).items():
        secret = secret_index.get(ref_name)
        secret_attrs = (
            {"keys": list(secret.key_names), "source_file": secret.source_file}
            if secret else None
        )
        _emit_reference(
            graph, node_id, NodeType.SECRET, "secret", dep_ns, ref_name, env,
            source_file, node_attrs=secret_attrs,
            signal=Signal.ENV_KEY_REF,
            keys=[r.key for r in refs],
            env_map={r.var: r.key for r in refs},
        )
        for r in refs:
            _emit_heuristic(graph, node_id, service_name, env, r.var, source_file)
    for ref_name, refs in _group_key_refs(container.configmap_key_refs).items():
        _emit_reference(
            graph, node_id, NodeType.CONFIGMAP, "cm", dep_ns, ref_name, env,
            source_file, signal=Signal.ENV_KEY_REF,
            keys=[r.key for r in refs],
            env_map={r.var: r.key for r in refs},
        )
        targets = index.discovery_configmaps.get(ref_name) or {}
        catalog = ref_name in index.catalog_configmaps
        for r in refs:
            target = targets.get(r.key)
            if target is None:
                continue
            dst = _ensure_service_target(graph, index, target)
            if dst == node_id:
                continue
            _emit_configmap_call(
                graph, node_id, dst, env, ref_name, r.key, source_file, catalog=catalog,
            )


def _env_literal_edges(
    graph: Graph,
    index: _ServiceIndex,
    node_id: str,
    container,
    env: Environment,
    source_file: str,
) -> None:
    """Literal env values -> explicit edges: a cluster DNS host is an http call
    (or calls_external through an ExternalName alias); any other URL is external."""
    for key, value in container.env.items():
        target = parse_cluster_host(value)
        if target is not None:
            dst = _ensure_service_target(graph, index, target)
            if dst != node_id:
                graph.add_edge(
                    Edge(
                        src_id=node_id,
                        dst_id=dst,
                        type=_call_edge_type(graph, dst),
                        environment=env,
                        provenance=Provenance.EXPLICIT,
                        signal=Signal.DNS_LITERAL,
                        source_file=source_file,
                        source_locator=key,
                    )
                )
        elif "://" in value and not is_cluster_internal(value):
            _emit_external(graph, node_id, env, key, value, source_file, Signal.ENV_LITERAL)


def _envfrom_secret_edges(
    graph: Graph,
    node_id: str,
    dep_ns: str,
    container,
    env: Environment,
    secret_index: dict,
    service_name: str,
) -> None:
    """Secrets consumed via envFrom: key-name heuristics (datastores/queues) plus
    a REFERENCES edge recording ALL key names + file location (never values), so
    keys without a datastore match aren't lost."""
    for secret_name in container.env_from_secrets:
        secret = secret_index.get(secret_name)
        if secret is None:
            continue
        for key in secret.key_names:
            _emit_heuristic(graph, node_id, service_name, env, key, secret.source_file)
        _emit_reference(
            graph, node_id, NodeType.SECRET, "secret",
            dep_ns, secret_name, env,
            secret.source_file,
            node_attrs={"keys": list(secret.key_names), "source_file": secret.source_file},
            signal=Signal.ENV_FROM,
        )


def _volume_config_edges(
    graph: Graph,
    index: _ServiceIndex,
    dep: DeploymentView,
    node_id: str,
    env: Environment,
    secret_index: dict,
) -> None:
    """Volume-mounted config (per deployment, not per container): a mounted
    discovery configmap still yields call edges; plain config/secrets become
    REFERENCES edges with their mount paths."""
    ns = graph.nodes[node_id].namespace or "default"
    for cm_name in dep.configmap_volumes:
        targets = index.discovery_configmaps.get(cm_name)
        if targets:  # a service-discovery configmap mounted as a file
            catalog = cm_name in index.catalog_configmaps
            for env_key, target in targets.items():
                dst = _ensure_service_target(graph, index, target)
                if dst == node_id:
                    continue
                _emit_configmap_call(
                    graph, node_id, dst, env, cm_name, env_key,
                    dep.resource.source_file, catalog=catalog,
                )
        else:  # plain config — record the dependency on the configmap
            _emit_reference(
                graph, node_id, NodeType.CONFIGMAP, "cm", ns, cm_name, env,
                dep.resource.source_file,
                mount_paths=dep.configmap_mount_paths.get(cm_name),
            )
    for secret_name in dep.secret_volumes:
        sv = secret_index.get(secret_name)
        # Store ONLY the key names and the Secret's own file location — never values.
        secret_attrs = (
            {"keys": list(sv.key_names), "source_file": sv.source_file} if sv else None
        )
        _emit_reference(
            graph, node_id, NodeType.SECRET, "secret", ns, secret_name, env,
            dep.resource.source_file, node_attrs=secret_attrs,
            mount_paths=dep.secret_mount_paths.get(secret_name),
        )


def _helmchart_edges(graph: Graph, result: ExtractResult, primary: str, env: Environment) -> None:
    """helmCharts -> explicit depends_on_chart (overlay-level -> primary node)."""
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
                src_id=primary,
                dst_id=chart_id,
                type=EdgeType.DEPENDS_ON_CHART,
                environment=env,
                provenance=Provenance.EXPLICIT,
                signal=Signal.HELMCHART,
                source_file=chart.source_file,
            )
        )


# --------------------------------------------------------------------------- #
# Routing — Istio VirtualService / Ingress -> ROUTES_TO
# --------------------------------------------------------------------------- #
def _resolve_routing_edges(
    graph: Graph,
    index: _ServiceIndex,
    result: ExtractResult,
    node_id: str,
    env: Environment,
) -> None:
    default_ns = graph.nodes[node_id].namespace or "default"
    for res in result.resources:
        if res.kind == "VirtualService":
            _virtualservice_routes(graph, index, res, node_id, env, default_ns)
        elif res.kind == "Ingress":
            _ingress_routes(graph, index, res, env, default_ns)


def _virtualservice_routes(
    graph: Graph,
    index: _ServiceIndex,
    res: K8sResource,
    node_id: str,
    env: Environment,
    default_ns: str,
) -> None:
    """The service owning the VS (typically a gateway) routes to each destination
    host declared under http/tcp/tls routes — an explicit, declared edge. When the
    VS is bound to Istio Gateways, those Gateways also route to the destinations."""
    spec = res.raw.get("spec") or {}
    gw_ids = [
        gid
        for gw in (spec.get("gateways") or [])
        if (gid := _ensure_gateway_node(graph, gw, default_ns)) is not None
    ]
    for proto in ("http", "tcp", "tls"):
        for rule in spec.get(proto) or []:
            if not isinstance(rule, dict):
                continue
            for route in rule.get("route") or []:
                destination = (route or {}).get("destination") or {}
                host = destination.get("host")
                dst = _resolve_route_host(graph, index, host, default_ns)
                if dst is None:
                    continue
                port = (destination.get("port") or {}).get("number")
                route_attrs = {"port": port} if isinstance(port, int) else {}
                if dst != node_id:
                    graph.add_edge(Edge(
                        src_id=node_id, dst_id=dst, type=EdgeType.ROUTES_TO, environment=env,
                        provenance=Provenance.EXPLICIT, signal=Signal.ISTIO_VS,
                        source_file=res.source_file, source_locator=str(host),
                        attrs=dict(route_attrs),
                    ))
                for gw_id in gw_ids:
                    if gw_id == dst:
                        continue
                    graph.add_edge(Edge(
                        src_id=gw_id, dst_id=dst, type=EdgeType.ROUTES_TO, environment=env,
                        provenance=Provenance.EXPLICIT, signal=Signal.GATEWAY_BINDING,
                        source_file=res.source_file, source_locator=str(host),
                        attrs=dict(route_attrs),
                    ))


def _ingress_routes(
    graph: Graph,
    index: _ServiceIndex,
    res: K8sResource,
    env: Environment,
    default_ns: str,
) -> None:
    """An Ingress maps an external host to a backend Service: ingress-host routes
    to that Service. Backends default to the Ingress' own namespace."""
    spec = res.raw.get("spec") or {}
    ns = res.namespace or default_ns
    rules = spec.get("rules") or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        host = rule.get("host") or res.name
        host_id = f"ingress-host:{host}"
        graph.add_node(Node(id=host_id, type=NodeType.INGRESS_HOST, name=str(host)))
        for path in ((rule.get("http") or {}).get("paths") or []):
            backend = (path or {}).get("backend") or {}
            svc_backend = backend.get("service") or {}
            name = svc_backend.get("name") or backend.get("serviceName")
            if not name:
                continue
            # networking.k8s.io/v1 uses service.port.{number,name}; the legacy
            # extensions/v1beta1 shape is a bare servicePort (int or name).
            port = (svc_backend.get("port") or {}) if isinstance(svc_backend.get("port"), dict) else {}
            route_attrs: dict = {}
            if isinstance(port.get("number"), int):
                route_attrs["port"] = port["number"]
            elif port.get("name"):
                route_attrs["port_name"] = str(port["name"])
            elif isinstance(backend.get("servicePort"), int):
                route_attrs["port"] = backend["servicePort"]
            elif backend.get("servicePort"):
                route_attrs["port_name"] = str(backend["servicePort"])
            dst = _ensure_service_target(graph, index, ClusterTarget(service=str(name), namespace=ns))
            graph.add_edge(Edge(
                src_id=host_id, dst_id=dst, type=EdgeType.ROUTES_TO, environment=env,
                provenance=Provenance.EXPLICIT, signal=Signal.INGRESS_BACKEND,
                source_file=res.source_file, source_locator=str(name),
                attrs=route_attrs,
            ))


def _resolve_route_host(
    graph: Graph, index: _ServiceIndex, host: object, default_ns: str
) -> str | None:
    if not isinstance(host, str) or not host or "*" in host:
        return None
    target = parse_cluster_host(host)
    if target is not None:
        return _ensure_service_target(graph, index, target)
    parts = host.split(".")
    if len(parts) >= 2:  # short FQDN like svc.namespace
        return _ensure_service_target(graph, index, ClusterTarget(service=parts[0], namespace=parts[1]))
    return _ensure_service_target(graph, index, ClusterTarget(service=host, namespace=default_ns))


# --------------------------------------------------------------------------- #
# NetworkPolicy — permitted connectivity -> ALLOWS_TO
# --------------------------------------------------------------------------- #
def _resolve_network_policy_edges(
    graph: Graph,
    index: _ServiceIndex,
    result: ExtractResult,
    node_id: str,
    env: Environment,
) -> None:
    """Emit ALLOWS_TO for selector-resolvable peers. A NetworkPolicy declares
    *permitted* connectivity, not an actual call — so it never becomes http_calls.
    ipBlock peers and port-only rules (no selector) are skipped: no service to
    resolve. The policy governs the workload its podSelector matches (else the
    overlay's primary node)."""
    default_ns = graph.nodes[node_id].namespace or "default"
    for res in result.resources:
        if res.kind != "NetworkPolicy":
            continue
        spec = res.raw.get("spec") or {}
        own_sel = (spec.get("podSelector") or {}).get("matchLabels") or {}
        owner = _match_selector(index, default_ns, {str(k): str(v) for k, v in own_sel.items()}) or node_id
        for rule in spec.get("egress") or []:
            for peer in (rule or {}).get("to") or []:
                dst = _resolve_netpol_peer(graph, index, peer, default_ns)
                if dst and dst != owner:
                    _emit_allows(graph, owner, dst, env, res.source_file, "egress")
        for rule in spec.get("ingress") or []:
            for peer in (rule or {}).get("from") or []:
                src = _resolve_netpol_peer(graph, index, peer, default_ns)
                if src and src != owner:
                    _emit_allows(graph, src, owner, env, res.source_file, "ingress")


def _resolve_netpol_peer(
    graph: Graph, index: _ServiceIndex, peer: object, default_ns: str
) -> str | None:
    if not isinstance(peer, dict) or "ipBlock" in peer:
        return None  # external CIDR — not a service
    ns_labels = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
    ns = str(ns_labels.get("kubernetes.io/metadata.name") or ns_labels.get("name") or default_ns)
    pod_labels = (peer.get("podSelector") or {}).get("matchLabels") or {}
    if not pod_labels:
        return None  # namespace-wide selector — too broad to attribute to a service
    # Match the selector against real pod labels (any keys, not just `app`).
    matched = _match_selector(index, ns, pod_labels)
    if matched is not None:
        return matched
    # No indexed workload matched — fall back to the conventional `app` key so an
    # edge to an out-of-repo / unindexed service still resolves.
    app = pod_labels.get("app") or pod_labels.get("app.kubernetes.io/name")
    if app:
        return _ensure_service_target(graph, index, ClusterTarget(service=str(app), namespace=ns))
    return None


def _match_selector(
    index: _ServiceIndex, ns: str, selector: dict[str, str]
) -> str | None:
    """Find the service whose pod labels are a superset of ``selector`` — the same
    rule Kubernetes uses to bind a Service/NetworkPolicy to its pods. Resolves only
    when the match is **unambiguous**: a shared `app` label (an api/worker/beat
    bundle) matches several workloads, so a non-specific selector returns None
    rather than picking one arbitrarily — the caller then falls back honestly."""
    if not selector:
        return None
    items = list(selector.items())
    matches = {
        node_id
        for labels, node_id in index.by_labels.get(ns, [])
        if all(labels.get(k) == v for k, v in items)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _emit_allows(
    graph: Graph, src_id: str, dst_id: str, env: Environment, source_file: str, locator: str
) -> None:
    graph.add_edge(Edge(
        src_id=src_id, dst_id=dst_id, type=EdgeType.ALLOWS_TO, environment=env,
        provenance=Provenance.EXPLICIT, signal=Signal.NETWORK_POLICY,
        source_file=source_file, source_locator=locator,
    ))


# --------------------------------------------------------------------------- #
# Structural infra — storage / autoscaling / identity / gateways / ownership
# --------------------------------------------------------------------------- #
def _resolve_storage_edges(
    graph: Graph,
    index: _ServiceIndex,
    result: ExtractResult,
    pairs: list[tuple[DeploymentView, str]],
    primary: str,
    env: Environment,
) -> None:
    """PersistentVolumeClaims become storage nodes; the *specific* workload that
    mounts them (or declares them via StatefulSet volumeClaimTemplates) gets the
    MOUNTS edge — so e.g. a celery-flower PVC attaches to flower, not the api."""
    ns = graph.nodes[primary].namespace or "default"
    for res in result.resources:
        if res.kind == "PersistentVolumeClaim" and res.name:
            _ensure_storage_node(graph, res.namespace or ns, res.name, res.raw)
    for dep, node_id in pairs:
        for claim in list(dep.pvc_volumes) + list(dep.volume_claim_templates):
            pvc_id = _ensure_storage_node(graph, ns, claim, None)
            paths = dep.pvc_mount_paths.get(claim)
            graph.add_edge(Edge(
                src_id=node_id, dst_id=pvc_id, type=EdgeType.MOUNTS, environment=env,
                provenance=Provenance.EXPLICIT, signal=Signal.VOLUME_CLAIM,
                source_file=dep.resource.source_file, source_locator=claim,
                attrs={"mount_paths": list(paths)} if paths else {},
            ))


def _ensure_storage_node(graph: Graph, ns: str, name: str, raw: dict | None) -> str:
    node_id = f"pvc:{ns}/{name}"
    attrs: dict = {}
    if isinstance(raw, dict):
        spec = raw.get("spec") or {}
        attrs["storage_class"] = spec.get("storageClassName")
        attrs["storage"] = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    graph.add_node(Node(id=node_id, type=NodeType.STORAGE, name=name, namespace=ns, attrs=attrs))
    return node_id


def _resolve_autoscaler_edges(
    graph: Graph, index: _ServiceIndex, result: ExtractResult, node_id: str, env: Environment
) -> None:
    """HorizontalPodAutoscaler and Keda ScaledObject become autoscaler nodes that
    SCALE their scaleTargetRef. Keda trigger *types* are kept; trigger metadata is
    dropped on purpose — it can carry hostnames/connection strings."""
    ns = graph.nodes[node_id].namespace or "default"
    for res in result.resources:
        if res.kind not in ("HorizontalPodAutoscaler", "ScaledObject"):
            continue
        spec = res.raw.get("spec") or {}
        ref = spec.get("scaleTargetRef") or {}
        target = ref.get("name")
        if not target:
            continue
        rns = res.namespace or ns
        hpa_id = f"hpa:{rns}/{res.name}"
        if res.kind == "ScaledObject":
            triggers = sorted({
                str(t["type"])
                for t in spec.get("triggers") or []
                if isinstance(t, dict) and t.get("type")
            })
            attrs = {
                "kind": "ScaledObject",
                "min_replicas": spec.get("minReplicaCount"),
                "max_replicas": spec.get("maxReplicaCount"),
                "target_kind": ref.get("kind") or "Deployment",
                "triggers": triggers,
            }
            signal = Signal.KEDA_SCALER
        else:
            attrs = {
                "min_replicas": spec.get("minReplicas"),
                "max_replicas": spec.get("maxReplicas"),
                "target_kind": ref.get("kind"),
            }
            signal = Signal.HPA_TARGET
        graph.add_node(Node(
            id=hpa_id, type=NodeType.AUTOSCALER, name=res.name, namespace=rns, attrs=attrs,
        ))
        dst = _ensure_service_target(graph, index, ClusterTarget(service=str(target), namespace=rns))
        graph.add_edge(Edge(
            src_id=hpa_id, dst_id=dst, type=EdgeType.SCALES, environment=env,
            provenance=Provenance.EXPLICIT, signal=signal,
            source_file=res.source_file, source_locator=str(target),
        ))


def _resolve_identity_edges(
    graph: Graph,
    index: _ServiceIndex,
    result: ExtractResult,
    pairs: list[tuple[DeploymentView, str]],
    primary: str,
    env: Environment,
) -> None:
    """ServiceAccounts become nodes; each workload's serviceAccountName is RUNS_AS."""
    ns = graph.nodes[primary].namespace or "default"
    for res in result.resources:
        if res.kind == "ServiceAccount" and res.name:
            _ensure_sa_node(graph, res.namespace or ns, res.name)
    for dep, node_id in pairs:
        sa = dep.service_account
        if not sa:
            continue
        sa_id = _ensure_sa_node(graph, ns, sa)
        graph.add_edge(Edge(
            src_id=node_id, dst_id=sa_id, type=EdgeType.RUNS_AS, environment=env,
            provenance=Provenance.EXPLICIT, signal=Signal.SERVICE_ACCOUNT_REF,
            source_file=dep.resource.source_file, source_locator=sa,
        ))


def _ensure_sa_node(graph: Graph, ns: str, name: str) -> str:
    node_id = f"sa:{ns}/{name}"
    graph.add_node(Node(id=node_id, type=NodeType.SERVICE_ACCOUNT, name=name, namespace=ns))
    return node_id


def _resolve_rbac_edges(
    graph: Graph, index: _ServiceIndex, result: ExtractResult, primary: str, env: Environment
) -> None:
    """Lightweight RBAC: a (Cluster)RoleBinding GRANTS its roleRef to each
    ServiceAccount subject. Only names (plus a rules count for standalone roles)
    are kept — never rule verbs/resources detail."""
    ns = graph.nodes[primary].namespace or "default"
    for res in result.resources:
        if res.kind in ("Role", "ClusterRole") and res.name:
            _ensure_role_node(
                graph,
                None if res.kind == "ClusterRole" else (res.namespace or ns),
                res.name,
                kind=res.kind,
                rules_count=len(res.raw.get("rules") or []),
            )
        if res.kind not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        ref = res.raw.get("roleRef") or {}
        if not isinstance(ref, dict) or not ref.get("name"):
            continue
        binding_ns = res.namespace or ns
        ref_kind = str(ref.get("kind") or "Role")
        role_id = _ensure_role_node(
            graph,
            None if ref_kind == "ClusterRole" else binding_ns,
            str(ref["name"]),
            kind=ref_kind,
        )
        for subject in res.raw.get("subjects") or []:
            if (
                not isinstance(subject, dict)
                or subject.get("kind") != "ServiceAccount"
                or not subject.get("name")
            ):
                continue
            sa_id = _ensure_sa_node(graph, str(subject.get("namespace") or binding_ns), str(subject["name"]))
            graph.add_edge(Edge(
                src_id=sa_id, dst_id=role_id, type=EdgeType.GRANTS, environment=env,
                provenance=Provenance.EXPLICIT, signal=Signal.ROLE_BINDING,
                source_file=res.source_file, source_locator=res.name,
            ))


def _ensure_role_node(
    graph: Graph, ns: str | None, name: str, *, kind: str, rules_count: int | None = None
) -> str:
    node_id = f"role:{ns or 'cluster'}/{name}"
    attrs: dict = {"kind": kind, "cluster_wide": ns is None}
    if rules_count is not None:
        attrs["rules_count"] = rules_count
    graph.add_node(Node(id=node_id, type=NodeType.ROLE, name=name, namespace=ns, attrs=attrs))
    return node_id


def _resolve_pdb_attrs(
    graph: Graph, index: _ServiceIndex, result: ExtractResult, primary: str
) -> None:
    """A PodDisruptionBudget protects the workload its selector matches: recorded
    as attrs on that node (no edge — it's a property, not a dependency). Ambiguous
    selectors (a shared bundle label) are skipped rather than guessed."""
    ns = graph.nodes[primary].namespace or "default"
    for res in result.resources:
        if res.kind != "PodDisruptionBudget" or not res.name:
            continue
        spec = res.raw.get("spec") or {}
        sel = (spec.get("selector") or {}).get("matchLabels") or {}
        target = _match_selector(index, res.namespace or ns, {str(k): str(v) for k, v in sel.items()})
        if target is None:
            continue
        graph.nodes[target].attrs["disruption_budget"] = {
            "name": res.name,
            "min_available": spec.get("minAvailable"),
            "max_unavailable": spec.get("maxUnavailable"),
            "source_file": res.source_file,
        }


def _resolve_gateway_edges(
    graph: Graph, index: _ServiceIndex, result: ExtractResult, node_id: str, env: Environment
) -> None:
    """Register Istio Gateway objects as nodes (VS binding edges are emitted in
    _virtualservice_routes)."""
    ns = graph.nodes[node_id].namespace or "default"
    for res in result.resources:
        if res.kind == "Gateway" and res.name:
            _ensure_gateway_node(graph, f"{res.namespace or ns}/{res.name}", ns, raw=res.raw)


def _ensure_gateway_node(
    graph: Graph, ref: object, default_ns: str, raw: dict | None = None
) -> str | None:
    if not isinstance(ref, str) or not ref or ref == "mesh":
        return None  # the implicit sidecar mesh, not a routable gateway node
    gns, name = (ref.split("/", 1) if "/" in ref else (default_ns, ref))
    node_id = f"gateway:{gns}/{name}"
    attrs: dict = {}
    if isinstance(raw, dict):
        attrs["selector"] = ((raw.get("spec") or {}).get("selector")) or None
    graph.add_node(Node(id=node_id, type=NodeType.GATEWAY, name=name, namespace=gns, attrs=attrs))
    return node_id


def _resolve_owner_edges(
    graph: Graph, index: _ServiceIndex, result: ExtractResult, primary: str, env: Environment
) -> None:
    """ownerReferences are normally populated by controllers at runtime, so static
    manifests rarely carry them — handled opportunistically when present and only
    when the owner resolves to a different indexed service."""
    ns = graph.nodes[primary].namespace or "default"
    for res in result.resources:
        owned = (index.by_alias.get((res.namespace or ns, res.name)) if res.name else None) or primary
        for owner in (res.raw.get("metadata") or {}).get("ownerReferences") or []:
            if not isinstance(owner, dict) or not owner.get("name"):
                continue
            owner_id = index.by_alias.get((ns, str(owner["name"])))
            if owner_id is None or owner_id == owned:
                continue
            graph.add_edge(Edge(
                src_id=owner_id, dst_id=owned, type=EdgeType.OWNS, environment=env,
                provenance=Provenance.EXPLICIT, signal=Signal.OWNER_REF,
                source_file=res.source_file, source_locator=str(owner.get("kind") or "owner"),
            ))


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


def _emit_configmap_call(
    graph: Graph,
    src_id: str,
    dst_id: str,
    env: Environment,
    cm_name: str,
    env_key: str,
    source_file: str,
    *,
    catalog: bool,
) -> None:
    """A service-discovery URL the service consumes. A URL from a *shared catalog*
    only proves reachability/availability, not a call, so it is heuristic; a URL
    from a configmap specific to this service is treated as an explicit call. If an
    independent explicit signal (e.g. a DNS literal) exists, dedup prefers it."""
    if catalog:
        provenance, confidence, attrs = Provenance.HEURISTIC, 0.55, {"shared_catalog": True}
    else:
        provenance, confidence, attrs = Provenance.EXPLICIT, 1.0, {}
    graph.add_edge(Edge(
        src_id=src_id, dst_id=dst_id, type=_call_edge_type(graph, dst_id), environment=env,
        provenance=provenance, signal=Signal.CONFIGMAP_URL, source_file=source_file,
        source_locator=f"{cm_name}.{env_key}", confidence=confidence, attrs=attrs,
    ))


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
    node_attrs: dict | None = None,
    signal: Signal = Signal.VOLUME_MOUNT,
    keys: list[str] | None = None,
    env_map: dict[str, str] | None = None,
    mount_paths: tuple[str, ...] | None = None,
) -> None:
    """Record that a service mounts/consumes a ConfigMap/Secret as config (no values read).

    ``node_attrs`` may carry the referenced object's own metadata — for a Secret
    the key/env-var *names* and the file it is defined in — never any secret value.
    ``signal`` distinguishes a volume mount from an ``envFrom`` / ``valueFrom``
    reference; ``keys`` are the key names this workload consumes (defaults to the
    referenced object's full key set) and ``env_map`` maps env var -> key for
    single-key ``valueFrom`` wiring. Names only, never values.
    """
    dst_id = f"{prefix}:{ns}/{name}"
    graph.add_node(Node(id=dst_id, type=node_type, name=name, namespace=ns, attrs=node_attrs or {}))
    # The edge already carries the environment and the app (src_id), so putting the
    # key NAMES here pins each variable to its (app, environment) — one edge per
    # (app, secret, env), so it never merges across environments like the node does.
    edge_attrs: dict = {"modes": [signal.value]}
    if keys is None and node_attrs and node_attrs.get("keys"):
        keys = list(node_attrs["keys"])
    if keys:
        edge_attrs["keys"] = list(dict.fromkeys(keys))
    if env_map:
        edge_attrs["env_map"] = dict(env_map)
    if mount_paths:
        edge_attrs["mount_paths"] = list(mount_paths)
    graph.add_edge(
        Edge(
            src_id=src_id,
            dst_id=dst_id,
            type=EdgeType.REFERENCES,
            environment=env,
            provenance=Provenance.EXPLICIT,
            signal=signal,
            source_file=source_file,
            # A stable, non-null locator keeps this edge's UNIQUE identity matchable
            # on re-sync (SQLite treats NULL locators as always-distinct, which would
            # otherwise churn the row every `sync-envs`).
            source_locator=name,
            attrs=edge_attrs,
        )
    )


def _ensure_external_node(graph: Graph, host: str) -> str:
    dst_id = f"ext:{host.replace('.', '-')}"
    graph.add_node(Node(id=dst_id, type=NodeType.EXTERNAL_API, name=host, attrs={"host": host}))
    return dst_id


def _call_edge_type(graph: Graph, dst_id: str) -> EdgeType:
    """An alias may resolve to an external node (ExternalName Service): calling
    through it is calls_external, not an in-cluster http_calls."""
    node = graph.nodes.get(dst_id)
    if node is not None and node.type == NodeType.EXTERNAL_API:
        return EdgeType.CALLS_EXTERNAL
    return EdgeType.HTTP_CALLS


def _emit_external(
    graph: Graph,
    src_id: str,
    env: Environment,
    key: str,
    value: str,
    source_file: str,
    signal: Signal,
) -> None:
    host = extract_host(value) or value
    dst_id = _ensure_external_node(graph, host)
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
