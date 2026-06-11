"""Phase-1 detail extraction: init/sidecar containers, probes, resources,
mount paths, bundle attrs, and the CronJob jobTemplate pod-spec path."""

from kubedian.domain.entities.graph import EdgeType
from tests.conftest import build_graph, repo_writer


def test_cronjob_pod_spec_lives_under_job_template(tmp_path):
    """A CronJob's containers/env live at spec.jobTemplate.spec.template.spec —
    they must be extracted (they used to be silently lost)."""
    repo = tmp_path / "r"
    w = repo_writer(repo)
    w(
        "backup/base/cronjob.yaml",
        """
        apiVersion: batch/v1
        kind: CronJob
        metadata:
          name: nightly-backup
          namespace: placeholder
        spec:
          schedule: "0 3 * * *"
          jobTemplate:
            spec:
              template:
                spec:
                  containers:
                    - name: backup
                      image: backup:1
                      env:
                        - name: POSTGRES_HOST
                          valueFrom:
                            secretKeyRef: {name: backup-secret, key: POSTGRES_HOST}
        """,
    )
    w("backup/overlays/staging/kustomization.yaml", "namespace: ops\nresources:\n  - ../../base\n")
    graph = build_graph(repo)
    node = graph.nodes["svc:ops/nightly-backup"]
    assert node.attrs["image"] == "backup:1"
    assert "POSTGRES_HOST" in node.attrs["env_vars"]
    # the secret ref and its key-name heuristic must fire from jobTemplate containers too
    assert any(
        e.type == EdgeType.REFERENCES and e.dst_id == "secret:ops/backup-secret"
        and e.src_id == "svc:ops/nightly-backup"
        for e in graph.edges
    )
    assert any(
        e.type == EdgeType.READS_FROM and e.src_id == "svc:ops/nightly-backup"
        for e in graph.edges
    )


def test_init_container_dependencies_and_role(tmp_path):
    """An init container (e.g. a migration) carries real dependencies: its
    valueFrom secret ref must emit a REFERENCES edge, and it is tagged role=init."""
    repo = tmp_path / "r"
    w = repo_writer(repo)
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
              initContainers:
                - name: migrate
                  image: migrate:1
                  env:
                    - name: DATABASE_URL
                      valueFrom:
                        secretKeyRef: {name: billing-secret, key: DATABASE_URL}
                - name: warm-cache
                  image: warmer:1
                  restartPolicy: Always
              containers:
                - name: api
                  image: api:1
                - name: istio-proxy
                  image: proxyv2:1
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n")
    graph = build_graph(repo)
    node = graph.nodes["svc:billing/billing"]
    roles = {c["name"]: c["role"] for c in node.attrs["containers"]}
    assert roles == {
        "api": "main",
        "istio-proxy": "sidecar",  # well-known injected proxy name
        "migrate": "init",
        "warm-cache": "sidecar",  # native sidecar: initContainer with restartPolicy Always
    }
    # the main container's image wins on the node (containers list order: main first)
    assert node.attrs["image"] == "api:1"
    refs = [
        e for e in graph.edges
        if e.type == EdgeType.REFERENCES and e.dst_id == "secret:billing/billing-secret"
    ]
    assert refs and refs[0].attrs.get("env_map") == {"DATABASE_URL": "DATABASE_URL"}


def test_probes_resources_and_bundle_attrs(tmp_path):
    repo = tmp_path / "r"
    w = repo_writer(repo)
    w(
        "shop/base/api.yaml",
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
                - name: api
                  image: shop:1
                  resources:
                    requests: {cpu: 100m, memory: 128Mi}
                    limits: {memory: 256Mi}
                  livenessProbe:
                    httpGet: {path: /health, port: 8000}
                  readinessProbe:
                    tcpSocket: {port: 8000}
                  startupProbe:
                    exec: {command: [cat, /tmp/ready]}
        """,
    )
    w("shop/overlays/staging/kustomization.yaml", "namespace: shop\nresources:\n  - ../../base\n")
    graph = build_graph(repo)
    attrs = graph.nodes["svc:shop/shop"].attrs
    assert attrs["overlay"] == "shop"
    assert attrs["app_label"] == "shop"
    (container,) = attrs["containers"]
    assert container["resources"] == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"memory": "256Mi"},
    }
    assert container["probes"] == {
        "liveness": {"type": "http", "port": 8000, "path": "/health"},
        "readiness": {"type": "tcp", "port": 8000},
        "startup": {"type": "exec"},
    }
    # exec probe must never carry the command contents
    assert "command" not in str(container["probes"])


def test_mount_paths_on_reference_and_mounts_edges(tmp_path):
    repo = tmp_path / "r"
    w = repo_writer(repo)
    w(
        "files/base/app.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: files
          namespace: placeholder
          labels: {app: files}
        spec:
          template:
            metadata:
              labels: {app: files}
            spec:
              containers:
                - name: app
                  image: files:1
                  volumeMounts:
                    - {name: app-config, mountPath: /etc/app}
                    - {name: app-tls, mountPath: /etc/tls}
                    - {name: data, mountPath: /var/data}
              volumes:
                - name: app-config
                  configMap: {name: files-config}
                - name: app-tls
                  secret: {secretName: files-tls}
                - name: data
                  persistentVolumeClaim: {claimName: files-data}
        """,
    )
    w(
        "files/base/config.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: files-config
          namespace: placeholder
        data:
          LOG_LEVEL: info
        """,
    )
    w("files/overlays/staging/kustomization.yaml", "namespace: files\nresources:\n  - ../../base\n")
    graph = build_graph(repo)
    refs = {e.dst_id: e for e in graph.edges if e.type == EdgeType.REFERENCES}
    assert refs["cm:files/files-config"].attrs["mount_paths"] == ["/etc/app"]
    assert refs["secret:files/files-tls"].attrs["mount_paths"] == ["/etc/tls"]
    (mount,) = [e for e in graph.edges if e.type == EdgeType.MOUNTS]
    assert mount.dst_id == "pvc:files/files-data"
    assert mount.attrs["mount_paths"] == ["/var/data"]
