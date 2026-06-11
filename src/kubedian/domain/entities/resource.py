"""Typed views over rendered Kubernetes objects.

These wrap raw parsed YAML dicts and expose only what the resolver needs.
The most important one is :class:`SecretView`: it structurally refuses to
return secret *values*, only key names — so encrypted/plaintext values can
never leak downstream into the graph, vault, or logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RenderMode(StrEnum):
    KUSTOMIZE = "kustomize"
    RAW_FALLBACK = "raw_fallback"


@dataclass(frozen=True)
class K8sResource:
    """A single rendered Kubernetes object plus where it came from."""

    kind: str
    name: str
    namespace: str | None
    raw: dict[str, Any]
    source_file: str
    render_mode: RenderMode

    @property
    def labels(self) -> dict[str, str]:
        return (self.raw.get("metadata") or {}).get("labels") or {}

    @property
    def annotations(self) -> dict[str, str]:
        return (self.raw.get("metadata") or {}).get("annotations") or {}


@dataclass(frozen=True)
class EnvKeyRef:
    """One env var wired to a single Secret/ConfigMap key via ``valueFrom``.

    Carries only NAMES (env var, referenced object, key) — never the value.
    """

    var: str  # env var name inside the container
    ref: str  # name of the referenced Secret/ConfigMap
    key: str  # key inside the referenced object


@dataclass(frozen=True)
class ContainerView:
    name: str
    image: str | None
    env: dict[str, str]  # only literal values (valueFrom is captured separately)
    env_from_secrets: list[str]
    env_from_configmaps: list[str]
    # env[].valueFrom.secretKeyRef / configMapKeyRef — names only, never values.
    secret_key_refs: tuple[EnvKeyRef, ...] = ()
    configmap_key_refs: tuple[EnvKeyRef, ...] = ()


@dataclass(frozen=True)
class DeploymentView:
    resource: K8sResource
    app_label: str | None
    # Pod-template labels — what Service/NetworkPolicy selectors actually match.
    pod_labels: dict[str, str] = field(default_factory=dict)
    containers: list[ContainerView] = field(default_factory=list)
    # Secret/ConfigMap names mounted as volumes (config-file-driven services).
    secret_volumes: tuple[str, ...] = ()
    configmap_volumes: tuple[str, ...] = ()
    # Distinguishing workload facts kept on the (collapsed) service node.
    workload_kind: str = "Deployment"
    replicas: int | None = None
    ports: tuple[int, ...] = ()
    # containerPort name -> number, for resolving Services' named targetPorts.
    named_ports: dict[str, int] = field(default_factory=dict)
    service_account: str | None = None
    # Node pool the workload is scheduled onto, from the `purpose` node label
    # (nodeSelector / required nodeAffinity). None when the workload doesn't pin a pool.
    nodepool: str | None = None
    # PersistentVolumeClaim names mounted, plus StatefulSet volumeClaimTemplates.
    pvc_volumes: tuple[str, ...] = ()
    volume_claim_templates: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecretView:
    """Exposes ONLY the key names of a Secret. Values are never stored here."""

    name: str
    namespace: str | None
    source_file: str
    _key_names: tuple[str, ...]

    @property
    def key_names(self) -> tuple[str, ...]:
        return self._key_names


@dataclass(frozen=True)
class HelmChartRef:
    name: str
    repo: str | None
    version: str | None
    namespace: str | None
    source_file: str
