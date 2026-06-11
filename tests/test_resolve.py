from pathlib import Path

from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.application.pipeline.resolve import resolve
from kubedian.domain.entities.graph import EdgeType, Environment, NodeType, Provenance, Signal
from tests.conftest import GATEWAY_SECRET_VALUE, write_sample_repo


def _graph_for(repo: Path):
    overlays = discover_overlays(repo, Environment.STAGING)
    results = [extract_overlay(o) for o in overlays]
    return resolve(results)


def test_explicit_http_call_from_configmap(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    http = [e for e in graph.edges if e.type == EdgeType.HTTP_CALLS]
    assert any(
        e.src_id == "svc:ns-a/service-a"
        and e.dst_id == "svc:ns-b/service-b"
        and e.provenance == Provenance.EXPLICIT
        and "SERVICE_B_API_URL" in (e.source_locator or "")
        for e in http
    ), [(e.src_id, e.dst_id, e.source_locator) for e in http]


def test_target_node_not_duplicated(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    service_b_nodes = [n for n in graph.nodes.values() if n.name == "service-b"]
    assert len(service_b_nodes) == 1
    assert service_b_nodes[0].id == "svc:ns-b/service-b"


def test_secret_key_heuristics_emit_datastores(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    out = {e.type for e in graph.edges if e.src_id == "svc:ns-a/service-a"}
    assert EdgeType.READS_FROM in out  # POSTGRES_HOST
    assert EdgeType.CACHES_IN in out  # REDIS_URI
    assert EdgeType.QUEUES_TO in out  # RABBITMQ_*
    # heuristic edges must be tagged as such, never explicit
    for e in graph.edges:
        if e.type in (EdgeType.READS_FROM, EdgeType.CACHES_IN, EdgeType.QUEUES_TO):
            assert e.provenance == Provenance.HEURISTIC
            assert e.confidence < 1.0


def test_rabbitmq_keys_dedup_to_single_queue_edge(tmp_path):
    """RABBITMQ_HOST + RABBITMQ_PORT must collapse to one queues_to edge."""
    graph = _graph_for(write_sample_repo(tmp_path))
    queue_edges = [
        e
        for e in graph.edges
        if e.src_id == "svc:ns-a/service-a" and e.type == EdgeType.QUEUES_TO
    ]
    assert len(queue_edges) == 1
    locators = queue_edges[0].attrs.get("locators") or []
    assert "RABBITMQ_HOST" in locators and "RABBITMQ_PORT" in locators


def test_configmap_generator_url_becomes_explicit_edge(tmp_path):
    """The link comes from the VALUE (cluster DNS URL), not the key name —
    even though the literal lives in the kustomization's configMapGenerator."""
    graph = _graph_for(write_sample_repo(tmp_path))
    http = [
        e
        for e in graph.edges
        if e.src_id == "svc:ns-gw/gateway"
        and e.dst_id == "svc:ns-b/service-b"
        and e.type == EdgeType.HTTP_CALLS
    ]
    assert http, [(e.src_id, e.dst_id, e.type) for e in graph.edges if "gateway" in e.src_id]
    assert http[0].provenance == Provenance.EXPLICIT
    assert "MS_CLIENT" in (http[0].source_locator or "")


def test_secret_generator_keeps_keyname_drops_value(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    # POSTGRES_HOST key drives a heuristic datastore edge...
    reads = [
        e for e in graph.edges
        if e.src_id == "svc:ns-gw/gateway" and e.type == EdgeType.READS_FROM
    ]
    assert reads and reads[0].provenance == Provenance.HEURISTIC
    # ...but the secret VALUE must never appear anywhere in the graph.
    blob = graph.model_dump_json()
    assert GATEWAY_SECRET_VALUE not in blob


def test_virtualservice_emits_routes_to(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    vs = [
        e for e in graph.edges
        if e.type == EdgeType.ROUTES_TO and e.signal == Signal.ISTIO_VS
        and e.src_id == "svc:ns-gw/gateway" and e.dst_id == "svc:ns-b/service-b"
    ]
    assert vs and vs[0].provenance == Provenance.EXPLICIT


def test_ingress_routes_from_host_node(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    ing = [
        e for e in graph.edges
        if e.type == EdgeType.ROUTES_TO and e.signal == Signal.INGRESS_BACKEND
    ]
    assert any(
        e.src_id == "ingress-host:gw.example.com" and e.dst_id == "svc:ns-gw/gateway"
        for e in ing
    ), [(e.src_id, e.dst_id) for e in ing]
    assert NodeType.INGRESS_HOST in {n.type for n in graph.nodes.values()}


def test_network_policy_emits_allows_to_not_http_calls(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    allows = [e for e in graph.edges if e.type == EdgeType.ALLOWS_TO]
    # egress: gateway -> service-b ; ingress: service-a -> gateway
    assert any(
        e.src_id == "svc:ns-gw/gateway" and e.dst_id == "svc:ns-b/service-b" for e in allows
    ), [(e.src_id, e.dst_id) for e in allows]
    assert any(
        e.src_id == "svc:ns-a/service-a" and e.dst_id == "svc:ns-gw/gateway" for e in allows
    ), [(e.src_id, e.dst_id) for e in allows]
    # permission is explicit but must stay its own type, never an http_call
    for e in allows:
        assert e.provenance == Provenance.EXPLICIT
        assert e.signal == Signal.NETWORK_POLICY


def test_netpol_peer_resolves_by_non_app_label(tmp_path):
    """The gateway's egress peer selects `component: api` (not `app`); it must
    still resolve to service-b via generic label-set matching."""
    graph = _graph_for(write_sample_repo(tmp_path))
    assert any(
        e.type == EdgeType.ALLOWS_TO
        and e.src_id == "svc:ns-gw/gateway"
        and e.dst_id == "svc:ns-b/service-b"
        for e in graph.edges
    ), [(e.src_id, e.dst_id, e.type) for e in graph.edges if e.type == EdgeType.ALLOWS_TO]


def test_multi_workload_overlay_splits_into_per_workload_nodes(tmp_path):
    """An overlay bundling several workloads (api + a flower StatefulSet) yields a
    node PER workload, and a sub-workload's PVC attaches to ITS node, not the api."""
    from textwrap import dedent

    repo = tmp_path / "m"

    def w(rel: str, text: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(text).lstrip(), encoding="utf-8")

    w(
        "billing/base/api.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: billing
          namespace: placeholder
          labels: {app: billing}
        spec:
          template:
            metadata:
              labels: {app: billing}
            spec:
              containers:
                - name: api
                  image: x:1
        """,
    )
    w(
        "billing/base/flower.yaml",
        """
        apiVersion: apps/v1
        kind: StatefulSet
        metadata:
          name: billing-flower
          namespace: placeholder
          labels: {app: billing-flower}
        spec:
          template:
            metadata:
              labels: {app: billing-flower}
            spec:
              containers:
                - name: flower
                  image: flower:1
          volumeClaimTemplates:
            - metadata: {name: flower-data}
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")

    graph = _graph_for(repo)
    assert "svc:billing/billing" in graph.nodes
    assert "svc:billing/billing-flower" in graph.nodes
    mounts = [(e.src_id, e.dst_id) for e in graph.edges if e.type == EdgeType.MOUNTS]
    # the flower PVC attaches to the flower node, never the api node
    assert ("svc:billing/billing-flower", "pvc:billing/flower-data") in mounts
    assert all(src != "svc:billing/billing" for src, _ in mounts)


def test_workload_facts_on_service_node(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    attrs = graph.nodes["svc:ns-data/data-store"].attrs
    assert attrs["workload_kind"] == "StatefulSet"
    assert attrs["replicas"] == 3
    assert attrs["ports"] == [5432]
    assert attrs["service_account"] == "data-sa"


def test_statefulset_pvc_becomes_storage_mount(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    assert "pvc:ns-data/data" in graph.nodes
    assert graph.nodes["pvc:ns-data/data"].type == NodeType.STORAGE
    assert any(
        e.type == EdgeType.MOUNTS
        and e.src_id == "svc:ns-data/data-store"
        and e.dst_id == "pvc:ns-data/data"
        and e.signal == Signal.VOLUME_CLAIM
        for e in graph.edges
    ), [(e.src_id, e.dst_id) for e in graph.edges if e.type == EdgeType.MOUNTS]


def test_hpa_becomes_autoscaler_that_scales(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    assert graph.nodes["hpa:ns-data/data-store"].type == NodeType.AUTOSCALER
    assert any(
        e.type == EdgeType.SCALES
        and e.src_id == "hpa:ns-data/data-store"
        and e.dst_id == "svc:ns-data/data-store"
        for e in graph.edges
    ), [(e.src_id, e.dst_id) for e in graph.edges if e.type == EdgeType.SCALES]


def test_service_account_runs_as(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    assert graph.nodes["sa:ns-data/data-sa"].type == NodeType.SERVICE_ACCOUNT
    assert any(
        e.type == EdgeType.RUNS_AS
        and e.src_id == "svc:ns-data/data-store"
        and e.dst_id == "sa:ns-data/data-sa"
        for e in graph.edges
    ), [(e.src_id, e.dst_id) for e in graph.edges if e.type == EdgeType.RUNS_AS]


def test_istio_gateway_routes_to_destination(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    assert graph.nodes["gateway:ns-gw/public-gw"].type == NodeType.GATEWAY
    assert any(
        e.type == EdgeType.ROUTES_TO
        and e.src_id == "gateway:ns-gw/public-gw"
        and e.dst_id == "svc:ns-b/service-b"
        and e.signal == Signal.GATEWAY_BINDING
        for e in graph.edges
    ), [(e.src_id, e.dst_id, e.signal) for e in graph.edges if e.type == EdgeType.ROUTES_TO]


def test_context_surfaces_structural_buckets(tmp_path):
    from kubedian.application.pipeline.index import index_repo
    from kubedian.application.use_cases import queries
    from kubedian.infrastructure.sqlite.graph_reader import GraphReader

    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    out = queries.context(GraphReader(db), "data-store", Environment.STAGING)
    assert out["storage"], out
    assert out["identity"], out
    assert out["autoscaling"], out
    # the HPA is surfaced under `autoscaler`, not the generic `service` key
    assert "autoscaler" in out["autoscaling"][0]
    assert "service" not in out["autoscaling"][0]
    assert out["service"]["workload_kind"] == "StatefulSet"
    assert out["service"]["replicas"] == 3
    assert 5432 in out["service"]["ports"]
    assert out["service"]["service_account"] == "data-sa"


def test_trace_follows_routing_through_gateway(tmp_path):
    from kubedian.application.pipeline.index import index_repo
    from kubedian.application.use_cases import queries
    from kubedian.infrastructure.sqlite.graph_reader import GraphReader

    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    reader = GraphReader(db)
    out = queries.trace(reader, "gateway", "service-b", Environment.STAGING)
    assert out["reachable"] is True
    assert out["path"][0] == "svc:ns-gw/gateway"
    assert out["path"][-1] == "svc:ns-b/service-b"


def test_shared_discovery_catalog_downgraded_to_heuristic(tmp_path):
    """A service-discovery ConfigMap shared by several services (a catalog) must
    NOT yield explicit http_calls — availability of a URL is not a call."""
    from textwrap import dedent

    repo = tmp_path / "m"

    def w(rel: str, text: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(text).lstrip(), encoding="utf-8")

    w("shared/overlays/staging/kustomization.yaml", "namespace: platform\nresources:\n  - cm.yaml\n")
    w(
        "shared/overlays/staging/cm.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: svc-catalog
          namespace: platform
        data:
          A_URL: http://svc-a.ns-a.svc.cluster.local
          B_URL: http://svc-b.ns-b.svc.cluster.local
          C_URL: http://svc-c.ns-c.svc.cluster.local
        """,
    )
    dep = """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: NAME
          namespace: placeholder
          labels:
            app: NAME
        spec:
          template:
            spec:
              containers:
                - name: api
                  image: x:1
                  envFrom:
                    - configMapRef:
                        name: svc-catalog
        """
    for name, ns in (("consumer-one", "c1"), ("consumer-two", "c2")):
        w(f"{name}/base/deployment.yaml", dep.replace("NAME", name))
        w(f"{name}/overlays/staging/kustomization.yaml", f"namespace: {ns}\nresources:\n  - ../../base\n")

    graph = _graph_for(repo)
    cat = [
        e for e in graph.edges
        if e.type == EdgeType.HTTP_CALLS and (e.attrs or {}).get("shared_catalog")
    ]
    assert cat, "expected catalog edges flagged shared_catalog"
    assert all(e.provenance == Provenance.HEURISTIC and e.confidence < 1.0 for e in cat)
    assert len(cat) == 6  # 2 consumers x 3 targets


def test_single_service_configmap_stays_explicit(tmp_path):
    """A discovery configmap consumed by only ONE service is its own config, not a
    shared catalog, so its edge stays explicit (regression guard)."""
    graph = _graph_for(write_sample_repo(tmp_path))
    e = next(
        e for e in graph.edges
        if e.src_id == "svc:ns-a/service-a" and e.dst_id == "svc:ns-b/service-b"
        and e.signal == Signal.CONFIGMAP_URL
    )
    assert e.provenance == Provenance.EXPLICIT
    assert not (e.attrs or {}).get("shared_catalog")


def test_ports_filled_from_service(tmp_path):
    """service-a's Deployment declares no containerPort; its Service's targetPort
    (8000) must populate the node's ports."""
    graph = _graph_for(write_sample_repo(tmp_path))
    assert graph.nodes["svc:ns-a/service-a"].attrs.get("ports") == [8000]


def test_jobs_and_cronjobs_get_their_own_node_type(tmp_path):
    """A migration Job / backup CronJob must not be typed as a service, but their
    dependency edges (a Job reading a DB) are still indexed."""
    from textwrap import dedent

    repo = tmp_path / "m"

    def w(rel: str, text: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(text).lstrip(), encoding="utf-8")

    w(
        "migrate/base/job.yaml",
        """
        apiVersion: batch/v1
        kind: Job
        metadata: {name: migrate, namespace: placeholder, labels: {app: migrate}}
        spec:
          template:
            spec:
              containers:
                - name: m
                  image: x:1
                  envFrom:
                    - secretRef: {name: migrate-secret}
        """,
    )
    w(
        "migrate/base/secret.yaml",
        """
        apiVersion: v1
        kind: Secret
        metadata: {name: migrate-secret, namespace: placeholder}
        stringData:
          POSTGRES_HOST: ENC[AES256_GCM,data:x==,tag:y==]
        """,
    )
    w("migrate/overlays/staging/kustomization.yaml", "namespace: jobs\nresources:\n  - ../../base\n")
    w(
        "backup/base/cronjob.yaml",
        """
        apiVersion: batch/v1
        kind: CronJob
        metadata: {name: backup, namespace: placeholder, labels: {app: backup}}
        spec:
          schedule: "0 0 * * *"
          jobTemplate:
            spec:
              template:
                spec:
                  containers:
                    - name: b
                      image: x:1
        """,
    )
    w("backup/overlays/staging/kustomization.yaml", "namespace: jobs\nresources:\n  - ../../base\n")

    graph = _graph_for(repo)
    assert graph.nodes["svc:jobs/migrate"].type == NodeType.JOB
    assert graph.nodes["svc:jobs/backup"].type == NodeType.CRONJOB
    # the Job's heuristic datastore dependency is preserved
    assert any(
        e.src_id == "svc:jobs/migrate" and e.type == EdgeType.READS_FROM for e in graph.edges
    )


def test_match_selector_requires_unique_match():
    """A shared `app` label (api/worker bundle) must not resolve arbitrarily."""
    from kubedian.application.pipeline.resolve import _ServiceIndex, _match_selector

    idx = _ServiceIndex()
    idx.by_labels["web"] = [
        ({"app": "web", "component": "api"}, "svc:web/web-api"),
        ({"app": "web", "component": "worker"}, "svc:web/web-worker"),
    ]
    assert _match_selector(idx, "web", {"app": "web"}) is None  # ambiguous
    assert _match_selector(idx, "web", {"app": "web", "component": "api"}) == "svc:web/web-api"
    assert _match_selector(idx, "web", {"app": "other"}) is None  # no match


def test_namespace_membership_edges(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    ns_edges = [e for e in graph.edges if e.type == EdgeType.IN_NAMESPACE]
    assert any(e.src_id == "svc:ns-a/service-a" and e.dst_id == "ns:ns-a" for e in ns_edges)
    assert NodeType.NAMESPACE in {n.type for n in graph.nodes.values()}


def test_nodepool_from_node_selector_and_affinity(tmp_path):
    """The `purpose` node label pins a workload to a pool: read it from
    nodeSelector (authoritative) and required nodeAffinity (fallback). A workload
    that pins no pool carries no nodepool attr."""
    from textwrap import dedent

    repo = tmp_path / "m"

    def w(rel: str, text: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(text).lstrip(), encoding="utf-8")

    w(
        "pool/base/api.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: api, namespace: placeholder, labels: {app: api}}
        spec:
          template:
            metadata:
              labels: {app: api}
            spec:
              nodeSelector: {purpose: high-mem}
              containers: [{name: api, image: x:1}]
        """,
    )
    w(
        "pool/base/worker.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: worker, namespace: placeholder, labels: {app: worker}}
        spec:
          template:
            metadata:
              labels: {app: worker}
            spec:
              affinity:
                nodeAffinity:
                  requiredDuringSchedulingIgnoredDuringExecution:
                    nodeSelectorTerms:
                      - matchExpressions:
                          - {key: purpose, operator: In, values: [gpu]}
              containers: [{name: worker, image: x:1}]
        """,
    )
    w(
        "pool/base/free.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: free, namespace: placeholder, labels: {app: free}}
        spec:
          template:
            metadata:
              labels: {app: free}
            spec:
              containers: [{name: free, image: x:1}]
        """,
    )
    w("pool/overlays/staging/kustomization.yaml", "namespace: pool\nresources:\n  - ../../base\n")

    graph = _graph_for(repo)
    assert graph.nodes["svc:pool/api"].attrs["nodepool"] == "high-mem"
    assert graph.nodes["svc:pool/worker"].attrs["nodepool"] == "gpu"
    # a workload that pins no pool has nodepool=None (not surfaced by node_dict)
    assert graph.nodes["svc:pool/free"].attrs["nodepool"] is None


# --------------------------------------------------------------------------- #
# valueFrom key refs, consumption modes, ports
# --------------------------------------------------------------------------- #
def _references(graph, src_id, dst_id):
    return [
        e for e in graph.edges
        if e.type == EdgeType.REFERENCES and e.src_id == src_id and e.dst_id == dst_id
    ]


def test_valuefrom_secret_key_ref_merges_with_envfrom(tmp_path):
    """service-a consumes service-a-secret BOTH via envFrom and via a single
    valueFrom secretKeyRef (DATABASE_HOST <- POSTGRES_HOST). One REFERENCES edge
    must survive, carrying the union of modes, all key names and the var->key map."""
    graph = _graph_for(write_sample_repo(tmp_path))
    refs = _references(graph, "svc:ns-a/service-a", "secret:ns-a/service-a-secret")
    assert len(refs) == 1, [(e.signal, e.attrs) for e in refs]
    edge = refs[0]
    assert set(edge.attrs["modes"]) == {Signal.ENV_FROM.value, Signal.ENV_KEY_REF.value}
    assert edge.attrs["env_map"] == {"DATABASE_HOST": "POSTGRES_HOST"}
    assert "POSTGRES_HOST" in edge.attrs["keys"]
    assert "REDIS_URI" in edge.attrs["keys"]  # from the envFrom full key set


def test_valuefrom_configmap_key_ref_edge(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    refs = _references(graph, "svc:ns-a/service-a", "cm:ns-a/service-a-config")
    assert len(refs) == 1
    edge = refs[0]
    assert edge.attrs["modes"] == [Signal.ENV_KEY_REF.value]
    assert edge.attrs["env_map"] == {"FEATURE_FLAG": "feature_flag"}
    assert edge.attrs["keys"] == ["feature_flag"]


def test_envfrom_configmap_emits_reference(tmp_path):
    """envFrom configMapRef now records the configmap dependency itself, even for
    a discovery configmap (the http_calls edges remain separate)."""
    graph = _graph_for(write_sample_repo(tmp_path))
    refs = _references(graph, "svc:ns-a/service-a", "cm:ns-a/service-discovery")
    assert len(refs) == 1
    assert Signal.ENV_FROM.value in refs[0].attrs["modes"]


def test_valuefrom_var_name_feeds_heuristics(tmp_path):
    """The env var NAME of a valueFrom ref (DATABASE_HOST) must drive the same
    key-name heuristics as envFrom keys; it dedups into the postgres edge."""
    graph = _graph_for(write_sample_repo(tmp_path))
    reads = [
        e for e in graph.edges
        if e.src_id == "svc:ns-a/service-a" and e.type == EdgeType.READS_FROM
    ]
    assert len(reads) == 1
    locators = reads[0].attrs.get("locators") or [reads[0].source_locator]
    assert "DATABASE_HOST" in locators or reads[0].source_locator == "DATABASE_HOST"
    assert "POSTGRES_HOST" in locators or "DATABASE_HOST" in locators


def test_workload_env_var_names_only(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    attrs = graph.nodes["svc:ns-a/service-a"].attrs
    assert attrs["env_vars"] == ["DATABASE_HOST", "FEATURE_FLAG"]


def test_service_port_map_resolves_named_target_port(tmp_path):
    """service-b's Service declares port 80 -> targetPort 'http'; the named port
    must resolve to the workload's containerPort 8080."""
    graph = _graph_for(write_sample_repo(tmp_path))
    node = graph.nodes["svc:ns-b/service-b"]
    assert node.attrs["ports"] == [8080]
    # both the service-b Service and the edge-proxy Service front this workload;
    # each contributes its own (tagged) wiring.
    assert {
        "name": "http", "port": 80, "target_port": 8080, "protocol": "TCP",
        "service": "service-b",
    } in node.attrs["port_map"]
    assert any(p["service"] == "edge-proxy" for p in node.attrs["port_map"])


def test_ingress_route_carries_backend_port(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    edges = [
        e for e in graph.edges
        if e.type == EdgeType.ROUTES_TO and e.signal == Signal.INGRESS_BACKEND
        and e.src_id == "ingress-host:gw.example.com"
    ]
    assert edges and edges[0].attrs.get("port") == 80


def test_virtualservice_route_carries_destination_port(tmp_path):
    graph = _graph_for(write_sample_repo(tmp_path))
    vs_edges = [
        e for e in graph.edges
        if e.type == EdgeType.ROUTES_TO
        and e.signal in (Signal.ISTIO_VS, Signal.GATEWAY_BINDING)
        and e.dst_id == "svc:ns-b/service-b"
    ]
    assert vs_edges
    for e in vs_edges:
        assert e.attrs.get("port") == 8080, (e.signal, e.attrs)
