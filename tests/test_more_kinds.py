"""Phase-2 kinds: ExternalName Services, PodDisruptionBudget, RBAC bindings,
Keda ScaledObjects, and the ignored-kinds counter in status()."""

from pathlib import Path
from textwrap import dedent

from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.application.pipeline.index import index_repo
from kubedian.application.pipeline.resolve import resolve
from kubedian.application.use_cases import queries
from kubedian.domain.entities.graph import EdgeType, Environment, NodeType, Signal
from kubedian.infrastructure.sqlite.graph_reader import GraphReader


def _graph_for(repo: Path):
    overlays = discover_overlays(repo, Environment.STAGING)
    results = [extract_overlay(o) for o in overlays]
    return resolve(results)


def _writer(repo: Path):
    def w(rel: str, text: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(text).lstrip(), encoding="utf-8")

    return w


def test_external_name_service_resolves_to_external_node(tmp_path):
    """A consumer of an ExternalName Service is really calling the external host:
    one calls_external edge to an ext: node, and no svc: ghost node is created."""
    repo = tmp_path / "r"
    w = _writer(repo)
    w(
        "shop/base/app.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: shop
          namespace: placeholder
          labels: {app: shop}
        spec:
          template:
            metadata:
              labels: {app: shop}
            spec:
              containers:
                - name: app
                  image: shop:1
                  env:
                    - name: PAYMENTS_URL
                      value: http://payments-proxy.shop.svc.cluster.local
        """,
    )
    w(
        "shop/base/externalname.yaml",
        """
        apiVersion: v1
        kind: Service
        metadata:
          name: payments-proxy
          namespace: placeholder
        spec:
          type: ExternalName
          externalName: api.payments.example.com
        """,
    )
    w("shop/overlays/staging/kustomization.yaml", "namespace: shop\nresources:\n  - ../../base\n")
    graph = _graph_for(repo)
    ext = graph.nodes["ext:api-payments-example-com"]
    assert ext.type == NodeType.EXTERNAL_API
    assert ext.attrs["host"] == "api.payments.example.com"
    assert ext.attrs["external_name_service"] == "payments-proxy"
    assert "svc:shop/payments-proxy" not in graph.nodes  # no ghost service node
    (call,) = [
        e for e in graph.edges
        if e.src_id == "svc:shop/shop" and e.dst_id == "ext:api-payments-example-com"
    ]
    assert call.type == EdgeType.CALLS_EXTERNAL
    assert call.signal == Signal.DNS_LITERAL
    assert call.source_locator == "PAYMENTS_URL"


def test_external_name_never_shadows_workload_alias(tmp_path):
    """An ExternalName Service named like an existing workload must not re-point
    the workload's consumers to the external node (setdefault semantics)."""
    repo = tmp_path / "r"
    w = _writer(repo)
    w(
        "shop/base/app.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: shop
          namespace: placeholder
          labels: {app: shop}
        spec:
          template:
            metadata:
              labels: {app: shop}
            spec:
              containers:
                - name: app
                  image: shop:1
        """,
    )
    w(
        "shop/base/externalname.yaml",
        """
        apiVersion: v1
        kind: Service
        metadata:
          name: shop
          namespace: placeholder
        spec:
          type: ExternalName
          externalName: legacy.example.com
        """,
    )
    w("shop/overlays/staging/kustomization.yaml", "namespace: shop\nresources:\n  - ../../base\n")
    graph = _graph_for(repo)
    assert graph.nodes["svc:shop/shop"].type == NodeType.SERVICE


def test_pdb_attrs_land_on_matched_workload_and_skip_ambiguous(tmp_path):
    repo = tmp_path / "r"
    w = _writer(repo)
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
              labels: {app: billing, component: api}
            spec:
              containers:
                - name: api
                  image: x:1
        """,
    )
    w(
        "billing/base/worker.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: billing-worker
          namespace: placeholder
          labels: {app: billing}
        spec:
          template:
            metadata:
              labels: {app: billing, component: worker}
            spec:
              containers:
                - name: worker
                  image: x:1
        """,
    )
    w(
        "billing/base/pdb.yaml",
        """
        apiVersion: policy/v1
        kind: PodDisruptionBudget
        metadata:
          name: billing-api-pdb
          namespace: placeholder
        spec:
          minAvailable: 1
          selector:
            matchLabels: {component: api}
        ---
        apiVersion: policy/v1
        kind: PodDisruptionBudget
        metadata:
          name: billing-ambiguous-pdb
          namespace: placeholder
        spec:
          maxUnavailable: 1
          selector:
            matchLabels: {app: billing}
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")
    graph = _graph_for(repo)
    pdb = graph.nodes["svc:billing/billing"].attrs["disruption_budget"]
    assert pdb["name"] == "billing-api-pdb"
    assert pdb["min_available"] == 1
    # the shared `app: billing` selector matches both workloads -> never guessed
    assert "disruption_budget" not in graph.nodes["svc:billing/billing-worker"].attrs


def test_rolebinding_grants_role_to_service_account(tmp_path):
    repo = tmp_path / "r"
    w = _writer(repo)
    w(
        "billing/base/rbac.yaml",
        """
        apiVersion: v1
        kind: ServiceAccount
        metadata:
          name: billing-sa
          namespace: placeholder
        ---
        apiVersion: rbac.authorization.k8s.io/v1
        kind: Role
        metadata:
          name: billing-reader
          namespace: placeholder
        rules:
          - apiGroups: [""]
            resources: [configmaps]
            verbs: [get, list]
        ---
        apiVersion: rbac.authorization.k8s.io/v1
        kind: RoleBinding
        metadata:
          name: billing-reader-binding
          namespace: placeholder
        roleRef: {kind: Role, name: billing-reader, apiGroup: rbac.authorization.k8s.io}
        subjects:
          - {kind: ServiceAccount, name: billing-sa}
        ---
        apiVersion: rbac.authorization.k8s.io/v1
        kind: ClusterRoleBinding
        metadata:
          name: billing-view-binding
        roleRef: {kind: ClusterRole, name: view, apiGroup: rbac.authorization.k8s.io}
        subjects:
          - {kind: ServiceAccount, name: billing-sa, namespace: billing}
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")
    graph = _graph_for(repo)
    role = graph.nodes["role:billing/billing-reader"]
    assert role.type == NodeType.ROLE
    assert role.attrs["rules_count"] == 1
    assert role.attrs["cluster_wide"] is False
    cluster_role = graph.nodes["role:cluster/view"]
    assert cluster_role.attrs["cluster_wide"] is True
    grants = {
        (e.src_id, e.dst_id)
        for e in graph.edges
        if e.type == EdgeType.GRANTS and e.signal == Signal.ROLE_BINDING
    }
    assert ("sa:billing/billing-sa", "role:billing/billing-reader") in grants
    assert ("sa:billing/billing-sa", "role:cluster/view") in grants
    # rule verbs/resources detail is never stored beyond the count
    assert "verbs" not in graph.model_dump_json()


def test_keda_scaledobject_scales_with_trigger_types_only(tmp_path):
    repo = tmp_path / "r"
    w = _writer(repo)
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
        "billing/base/scaledobject.yaml",
        """
        apiVersion: keda.sh/v1alpha1
        kind: ScaledObject
        metadata:
          name: billing-scaler
          namespace: placeholder
        spec:
          scaleTargetRef: {name: billing}
          minReplicaCount: 1
          maxReplicaCount: 10
          triggers:
            - type: rabbitmq
              metadata:
                host: amqp://user:SECRETPASS@rabbit.internal:5672
                queueName: billing-tasks
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")
    graph = _graph_for(repo)
    scaler = graph.nodes["hpa:billing/billing-scaler"]
    assert scaler.type == NodeType.AUTOSCALER
    assert scaler.attrs["kind"] == "ScaledObject"
    assert scaler.attrs["min_replicas"] == 1 and scaler.attrs["max_replicas"] == 10
    assert scaler.attrs["triggers"] == ["rabbitmq"]
    assert any(
        e.type == EdgeType.SCALES and e.src_id == "hpa:billing/billing-scaler"
        and e.dst_id == "svc:billing/billing" and e.signal == Signal.KEDA_SCALER
        for e in graph.edges
    )
    # trigger metadata may carry connection strings — it must never reach the graph
    assert "SECRETPASS" not in graph.model_dump_json()


def test_status_counts_ignored_kinds(tmp_path):
    repo = tmp_path / "r"
    w = _writer(repo)
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
        "billing/base/destinationrule.yaml",
        """
        apiVersion: networking.istio.io/v1beta1
        kind: DestinationRule
        metadata:
          name: billing-dr
          namespace: placeholder
        spec:
          host: billing
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")
    db = tmp_path / "graph.db"
    report = index_repo(repo, db, Environment.STAGING)
    assert report.ignored_kinds == {"DestinationRule": 1}
    reader = GraphReader(db)
    try:
        st = queries.status(reader)
        assert st["ignored_kinds"] == {"DestinationRule": 1}
        assert st["ignored_kind_count"] == 1
    finally:
        reader.close()
