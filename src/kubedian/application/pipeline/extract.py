"""Stages 1+2 — render an overlay and extract typed Kubernetes resources.

Render with ``kustomize build`` when possible (resolves namespaces/patches);
on failure fall back to parsing the raw YAML files of the overlay and its base
directory. Every resource records which ``render_mode`` produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kubedian.application.pipeline.discover import Overlay
from kubedian.domain.entities.resource import (
    ContainerView,
    DeploymentView,
    HelmChartRef,
    K8sResource,
    RenderMode,
    SecretView,
)
from kubedian.infrastructure import yaml_io
from kubedian.infrastructure.kustomize.runner import (
    KustomizeNotFound,
    RenderResult,
    render_overlay,
)
from kubedian.infrastructure.sanitize import secret_key_names

_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


@dataclass
class ExtractResult:
    overlay: Overlay
    render_mode: RenderMode
    render_error: str | None
    # The overlay's declared `namespace:` — kustomize's namespace transformer makes
    # this authoritative for every resource, so we trust it over hardcoded values.
    overlay_namespace: str | None = None
    resources: list[K8sResource] = field(default_factory=list)
    deployments: list[DeploymentView] = field(default_factory=list)
    secrets: list[SecretView] = field(default_factory=list)
    helm_charts: list[HelmChartRef] = field(default_factory=list)


def extract_overlay(overlay: Overlay) -> ExtractResult:
    overlay_ns = _overlay_namespace(overlay)
    try:
        render = render_overlay(overlay.directory)
    except KustomizeNotFound:
        render = RenderResult(ok=False, yaml_text="", stderr="kustomize not installed")
    if render.ok:
        docs = list(yaml_io.load_all_text(render.yaml_text))
        mode = RenderMode.KUSTOMIZE
        error = None
        source = str(overlay.kustomization)
        result = _build(overlay, docs, mode, error, default_source=source)
    else:
        docs, sources = _raw_fallback_docs(overlay, overlay_ns)
        mode = RenderMode.RAW_FALLBACK
        error = render.stderr or "kustomize build failed"
        result = _build(overlay, docs, mode, error, doc_sources=sources)

    result.overlay_namespace = overlay_ns
    # helmCharts[] always come from the kustomization file declaratively.
    result.helm_charts = _extract_helm_charts(overlay)
    return result


def _build(
    overlay: Overlay,
    docs: list[dict[str, Any]],
    mode: RenderMode,
    error: str | None,
    *,
    default_source: str | None = None,
    doc_sources: list[str] | None = None,
) -> ExtractResult:
    result = ExtractResult(overlay=overlay, render_mode=mode, render_error=error)
    for i, doc in enumerate(docs):
        kind = doc.get("kind")
        if not kind:
            continue
        meta = doc.get("metadata") or {}
        name = meta.get("name") or ""
        namespace = meta.get("namespace")
        source = (
            doc_sources[i] if doc_sources and i < len(doc_sources) else default_source
        ) or str(overlay.kustomization)
        res = K8sResource(
            kind=kind,
            name=name,
            namespace=namespace,
            raw=doc,
            source_file=source,
            render_mode=mode,
        )
        result.resources.append(res)
        if kind in _WORKLOAD_KINDS:
            result.deployments.append(_deployment_view(res))
        elif kind == "Secret":
            result.secrets.append(
                SecretView(
                    name=name,
                    namespace=namespace,
                    source_file=source,
                    _key_names=secret_key_names(doc),
                )
            )
    return result


def _deployment_view(res: K8sResource) -> DeploymentView:
    spec = res.raw.get("spec") or {}
    template = spec.get("template") or {}
    pod_spec = template.get("spec") or {}
    containers_raw = pod_spec.get("containers") or []
    containers: list[ContainerView] = []
    for c in containers_raw:
        env_literals: dict[str, str] = {}
        for e in c.get("env") or []:
            if isinstance(e, dict) and "value" in e and "name" in e:
                env_literals[str(e["name"])] = str(e["value"])
        secret_refs: list[str] = []
        cm_refs: list[str] = []
        for ef in c.get("envFrom") or []:
            if not isinstance(ef, dict):
                continue
            if (sr := ef.get("secretRef")) and sr.get("name"):
                secret_refs.append(str(sr["name"]))
            if (cr := ef.get("configMapRef")) and cr.get("name"):
                cm_refs.append(str(cr["name"]))
        containers.append(
            ContainerView(
                name=str(c.get("name") or ""),
                image=c.get("image"),
                env=env_literals,
                env_from_secrets=secret_refs,
                env_from_configmaps=cm_refs,
            )
        )
    secret_vols: list[str] = []
    cm_vols: list[str] = []
    for vol in pod_spec.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        if (s := vol.get("secret")) and s.get("secretName"):
            secret_vols.append(str(s["secretName"]))
        if (cm := vol.get("configMap")) and cm.get("name"):
            cm_vols.append(str(cm["name"]))

    app_label = res.labels.get("app") or res.labels.get("app.kubernetes.io/name")
    return DeploymentView(
        resource=res,
        app_label=app_label,
        containers=containers,
        secret_volumes=tuple(dict.fromkeys(secret_vols)),
        configmap_volumes=tuple(dict.fromkeys(cm_vols)),
    )


def _extract_helm_charts(overlay: Overlay) -> list[HelmChartRef]:
    charts: list[HelmChartRef] = []
    for doc in yaml_io.load_all_file(overlay.kustomization):
        for chart in doc.get("helmCharts") or []:
            if not isinstance(chart, dict) or not chart.get("name"):
                continue
            charts.append(
                HelmChartRef(
                    name=str(chart["name"]),
                    repo=chart.get("repo"),
                    version=str(chart["version"]) if chart.get("version") else None,
                    namespace=chart.get("namespace"),
                    source_file=str(overlay.kustomization),
                )
            )
    return charts


def _raw_fallback_docs(
    overlay: Overlay, overlay_ns: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse YAML from the overlay dir and the sibling base dir directly.

    Less accurate than kustomize (no patch resolution) but keeps the index alive
    when an overlay won't build (common here: the ksops SOPS generator is an
    external plugin kustomize refuses to load). We replicate kustomize's
    *namespace transformer* by forcing the overlay's declared namespace onto
    every resource — making the namespace authoritative as kustomize would.
    """
    search_dirs = [overlay.directory]
    service_root = _service_root(overlay.directory)
    if service_root is not None:
        base = service_root / "base"
        if base.is_dir():
            search_dirs.append(base)

    docs: list[dict[str, Any]] = []
    sources: list[str] = []
    seen: set[Path] = set()
    for d in search_dirs:
        for path in sorted(d.rglob("*.y*ml")):
            if path in seen or path.name.lower().startswith("kustomization"):
                continue
            seen.add(path)
            for doc in yaml_io.load_all_file(path):
                if isinstance(doc, dict) and overlay_ns:
                    meta = doc.setdefault("metadata", {})
                    if isinstance(meta, dict):
                        meta["namespace"] = overlay_ns
                docs.append(doc)
                sources.append(str(path))
    return docs, sources


def _service_root(overlay_dir: Path) -> Path | None:
    for parent in overlay_dir.parents:
        if parent.name == "overlays":
            return parent.parent
    return None


def _overlay_namespace(overlay: Overlay) -> str | None:
    for doc in yaml_io.load_all_file(overlay.kustomization):
        ns = doc.get("namespace")
        if ns:
            return str(ns)
    return None
