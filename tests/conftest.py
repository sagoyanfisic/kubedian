"""Shared test fixtures: a tiny synthetic manifests repo.

Layout mirrors the real one: a discovery ConfigMap in one overlay, two services
with base + overlays, an envFrom secretRef pointing at a SOPS-style secret whose
values are encrypted (ENC[...]) and whose key names drive heuristics.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.application.pipeline.resolve import resolve
from kubedian.domain.entities.graph import Environment


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip(), encoding="utf-8")


def repo_writer(repo: Path):
    """Writer for inline test repos: ``w("svc/base/x.yaml", yaml_text)``."""

    def w(rel: str, text: str) -> None:
        _w(repo / rel, text)

    return w


def build_graph(repo: Path, environment: Environment = Environment.STAGING):
    """Run discover → extract → resolve over a test repo."""
    overlays = discover_overlays(repo, environment)
    return resolve([extract_overlay(o) for o in overlays])


def write_sample_repo(root: Path) -> Path:
    repo = root / "manifests"

    # --- shared discovery configmap (its own discoverable overlay) -----------
    _w(
        repo / "shared-elements/overlays/staging/kustomization.yaml",
        """
        namespace: platform
        resources:
          - config-maps.yaml
        """,
    )
    _w(
        repo / "shared-elements/overlays/staging/config-maps.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: service-discovery
          namespace: platform
        data:
          SERVICE_B_API_URL: http://service-b.ns-b.svc.cluster.local
        """,
    )

    # --- service-a: consumes the configmap + a SOPS secret -------------------
    _w(
        repo / "service-a/base/deployment.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: service-a
          namespace: placeholder
          labels:
            app: service-a
        spec:
          template:
            spec:
              containers:
                - name: api
                  image: example/service-a:latest
                  env:
                    - name: DATABASE_HOST
                      valueFrom:
                        secretKeyRef:
                          name: service-a-secret
                          key: POSTGRES_HOST
                    - name: FEATURE_FLAG
                      valueFrom:
                        configMapKeyRef:
                          name: service-a-config
                          key: feature_flag
                  envFrom:
                    - configMapRef:
                        name: service-discovery
                    - secretRef:
                        name: service-a-secret
        """,
    )
    _w(
        repo / "service-a/base/service.yaml",
        """
        apiVersion: v1
        kind: Service
        metadata:
          name: service-a
          namespace: placeholder
        spec:
          selector:
            app: service-a
          ports:
            - port: 80
              targetPort: 8000
        """,
    )
    _w(
        repo / "service-a/overlays/staging/kustomization.yaml",
        """
        namespace: ns-a
        resources:
          - ../../base
          - secrets.yaml
          - config.yaml
        """,
    )
    # Plain (non-discovery) configmap consumed key-by-key via valueFrom.
    _w(
        repo / "service-a/overlays/staging/config.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: service-a-config
          namespace: ns-a
        data:
          feature_flag: "on"
          log_level: "info"
        """,
    )
    # SOPS-style secret: plaintext KEYS, encrypted VALUES.
    _w(
        repo / "service-a/overlays/staging/secrets.yaml",
        """
        apiVersion: v1
        kind: Secret
        metadata:
          name: service-a-secret
          namespace: ns-a
        stringData:
          POSTGRES_HOST: ENC[AES256_GCM,data:aaa==,tag:bbb==]
          REDIS_URI: ENC[AES256_GCM,data:ccc==,tag:ddd==]
          RABBITMQ_HOST: ENC[AES256_GCM,data:eee==,tag:fff==]
          RABBITMQ_PORT: ENC[AES256_GCM,data:ggg==,tag:hhh==]
          EMAIL_API_URL: ENC[AES256_GCM,data:iii==,tag:jjj==]
        sops:
          encrypted_regex: ^(data|stringData)$
        """,
    )

    # --- service-b: the http_calls target ------------------------------------
    _w(
        repo / "service-b/base/deployment.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: service-b
          namespace: placeholder
          labels:
            app: service-b
        spec:
          template:
            metadata:
              labels:
                app: service-b
                component: api
            spec:
              containers:
                - name: api
                  image: example/service-b:latest
                  ports:
                    - name: http
                      containerPort: 8080
        """,
    )
    _w(
        repo / "service-b/base/service.yaml",
        """
        apiVersion: v1
        kind: Service
        metadata:
          name: service-b
          namespace: placeholder
        spec:
          selector:
            app: service-b
          ports:
            - name: http
              port: 80
              targetPort: http
        """,
    )
    _w(
        repo / "service-b/overlays/staging/kustomization.yaml",
        """
        namespace: ns-b
        resources:
          - ../../base
        """,
    )

    # --- gateway: routing (Istio VS + Ingress), generators, NetworkPolicy -----
    # Its overlay references a missing file → forced raw fallback, which is where
    # configMapGenerator/secretGenerator literals would otherwise be lost.
    _w(
        repo / "gateway/base/deployment.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: gateway
          namespace: placeholder
          labels:
            app: gateway
        spec:
          template:
            spec:
              containers:
                - name: api
                  image: example/gateway:latest
                  envFrom:
                    - configMapRef:
                        name: gateway-cm
                    - secretRef:
                        name: gateway-sc
        """,
    )
    _w(
        repo / "gateway/base/virtualservice.yaml",
        """
        apiVersion: networking.istio.io/v1beta1
        kind: VirtualService
        metadata:
          name: gateway
          namespace: placeholder
        spec:
          hosts:
            - gateway.example.com
          gateways:
            - public-gw
          http:
            - route:
                - destination:
                    host: service-b.ns-b.svc.cluster.local
                    port:
                      number: 8080
        """,
    )
    _w(
        repo / "gateway/base/gateway.yaml",
        """
        apiVersion: networking.istio.io/v1beta1
        kind: Gateway
        metadata:
          name: public-gw
          namespace: placeholder
        spec:
          selector:
            istio: ingressgateway
          servers:
            - port:
                number: 443
                name: https
                protocol: HTTPS
              hosts:
                - gateway.example.com
        """,
    )
    _w(
        repo / "gateway/base/ingress.yaml",
        """
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: gateway
          namespace: placeholder
        spec:
          rules:
            - host: gw.example.com
              http:
                paths:
                  - path: /
                    pathType: Prefix
                    backend:
                      service:
                        name: gateway
                        port:
                          number: 80
        """,
    )
    _w(
        repo / "gateway/base/network-policy.yaml",
        """
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        metadata:
          name: gateway
          namespace: placeholder
        spec:
          podSelector:
            matchLabels:
              app: gateway
          policyTypes:
            - Ingress
            - Egress
          egress:
            - to:
                - namespaceSelector:
                    matchLabels:
                      kubernetes.io/metadata.name: ns-b
                  podSelector:
                    matchLabels:
                      component: api  # non-`app` key: resolves via label-set match
          ingress:
            - from:
                - namespaceSelector:
                    matchLabels:
                      kubernetes.io/metadata.name: ns-a
                  podSelector:
                    matchLabels:
                      app: service-a
              ports:
                - port: 8000
        """,
    )
    # The literal KEY name (MS_CLIENT) is arbitrary; the edge comes from the VALUE
    # being an in-cluster DNS URL. The secretGenerator value must never leak.
    _w(
        repo / "gateway/overlays/staging/kustomization.yaml",
        """
        namespace: ns-gw
        resources:
          - ../../base
          - does-not-exist.yaml
        configMapGenerator:
          - name: gateway-cm
            literals:
              - MS_CLIENT=http://service-b.ns-b.svc.cluster.local/graphql
              - PLAYGROUND="true"
        secretGenerator:
          - name: gateway-sc
            literals:
              - POSTGRES_HOST=super-secret-db-host.internal
        """,
    )

    # --- edge-proxy: a Service in ns-b that fronts service-b's pods by label ---
    # Its name differs from the workload it selects, so it exercises SELECTS.
    _w(
        repo / "edge-proxy/base/service.yaml",
        """
        apiVersion: v1
        kind: Service
        metadata:
          name: edge-proxy
          namespace: placeholder
        spec:
          selector:
            app: service-b
          ports:
            - port: 80
        """,
    )
    _w(
        repo / "edge-proxy/overlays/staging/kustomization.yaml",
        """
        namespace: ns-b
        resources:
          - ../../base
        """,
    )

    # --- data-store: StatefulSet exercising storage (PVC), HPA, ServiceAccount --
    _w(
        repo / "data-store/base/statefulset.yaml",
        """
        apiVersion: apps/v1
        kind: StatefulSet
        metadata:
          name: data-store
          namespace: placeholder
          labels:
            app: data-store
        spec:
          replicas: 3
          template:
            metadata:
              labels:
                app: data-store
            spec:
              serviceAccountName: data-sa
              containers:
                - name: db
                  image: example/data-store:latest
                  ports:
                    - containerPort: 5432
          volumeClaimTemplates:
            - metadata:
                name: data
              spec:
                storageClassName: gp2
                resources:
                  requests:
                    storage: 10Gi
        """,
    )
    _w(
        repo / "data-store/base/serviceaccount.yaml",
        """
        apiVersion: v1
        kind: ServiceAccount
        metadata:
          name: data-sa
          namespace: placeholder
        """,
    )
    _w(
        repo / "data-store/base/hpa.yaml",
        """
        apiVersion: autoscaling/v2
        kind: HorizontalPodAutoscaler
        metadata:
          name: data-store
          namespace: placeholder
        spec:
          scaleTargetRef:
            apiVersion: apps/v1
            kind: StatefulSet
            name: data-store
          minReplicas: 3
          maxReplicas: 10
        """,
    )
    _w(
        repo / "data-store/overlays/staging/kustomization.yaml",
        """
        namespace: ns-data
        resources:
          - ../../base
        """,
    )
    return repo


# Secret value planted in the gateway's secretGenerator; must never reach the graph.
GATEWAY_SECRET_VALUE = "super-secret-db-host.internal"
