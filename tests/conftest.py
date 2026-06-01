"""Shared test fixtures: a tiny synthetic manifests repo.

Layout mirrors the real one: a discovery ConfigMap in one overlay, two services
with base + overlays, an envFrom secretRef pointing at a SOPS-style secret whose
values are encrypted (ENC[...]) and whose key names drive heuristics.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip(), encoding="utf-8")


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
            spec:
              containers:
                - name: api
                  image: example/service-b:latest
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
            - port: 80
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
    return repo
