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
class ContainerView:
    name: str
    image: str | None
    env: dict[str, str]  # only literal values (valueFrom is captured separately)
    env_from_secrets: list[str]
    env_from_configmaps: list[str]


@dataclass(frozen=True)
class DeploymentView:
    resource: K8sResource
    app_label: str | None
    containers: list[ContainerView] = field(default_factory=list)
    # Secret/ConfigMap names mounted as volumes (config-file-driven services).
    secret_volumes: tuple[str, ...] = ()
    configmap_volumes: tuple[str, ...] = ()


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
