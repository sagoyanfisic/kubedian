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

# The node label whose value names the node pool a workload is pinned to.
_NODEPOOL_LABEL = "purpose"


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
    ports: list[int] = []
    for c in containers_raw:
        for p in c.get("ports") or []:
            if isinstance(p, dict) and isinstance(p.get("containerPort"), int):
                ports.append(p["containerPort"])
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
    pvc_vols: list[str] = []
    for vol in pod_spec.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        if (s := vol.get("secret")) and s.get("secretName"):
            secret_vols.append(str(s["secretName"]))
        if (cm := vol.get("configMap")) and cm.get("name"):
            cm_vols.append(str(cm["name"]))
        if (pvc := vol.get("persistentVolumeClaim")) and pvc.get("claimName"):
            pvc_vols.append(str(pvc["claimName"]))
    # StatefulSet declares its PVCs inline via volumeClaimTemplates.
    vct_names = [
        str(t["metadata"]["name"])
        for t in spec.get("volumeClaimTemplates") or []
        if isinstance(t, dict) and (t.get("metadata") or {}).get("name")
    ]

    app_label = res.labels.get("app") or res.labels.get("app.kubernetes.io/name")
    # Selectors match the pod template's labels; fall back to the workload's own
    # labels when the template omits them (common in terse manifests).
    pod_labels = dict((template.get("metadata") or {}).get("labels") or {}) or dict(res.labels)
    replicas = spec.get("replicas")
    return DeploymentView(
        resource=res,
        app_label=app_label,
        pod_labels=pod_labels,
        containers=containers,
        secret_volumes=tuple(dict.fromkeys(secret_vols)),
        configmap_volumes=tuple(dict.fromkeys(cm_vols)),
        workload_kind=res.kind,
        replicas=replicas if isinstance(replicas, int) else None,
        ports=tuple(dict.fromkeys(ports)),
        service_account=pod_spec.get("serviceAccountName"),
        nodepool=_nodepool(pod_spec),
        pvc_volumes=tuple(dict.fromkeys(pvc_vols)),
        volume_claim_templates=tuple(dict.fromkeys(vct_names)),
    )


def _nodepool(pod_spec: dict[str, Any]) -> str | None:
    """The node pool a workload pins itself to, from the `purpose` node label.

    A ``nodeSelector`` schedules the pod onto matching nodes (a hard placement),
    so it's the authoritative signal. A required ``nodeAffinity`` term means the
    same thing in richer syntax; we read it as a fallback. Tolerations are skipped
    on purpose — a toleration only *permits* scheduling onto a tainted pool, it
    doesn't pin to one (permission, not placement).
    """
    selector = pod_spec.get("nodeSelector")
    if isinstance(selector, dict) and selector.get(_NODEPOOL_LABEL):
        return str(selector[_NODEPOOL_LABEL])
    affinity = (pod_spec.get("affinity") or {}).get("nodeAffinity") or {}
    required = affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in required.get("nodeSelectorTerms") or []:
        for expr in (term or {}).get("matchExpressions") or []:
            if not isinstance(expr, dict) or expr.get("key") != _NODEPOOL_LABEL:
                continue
            values = expr.get("values") or []
            if expr.get("operator") == "In" and values:
                return str(values[0])
    return None


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

    # configMapGenerator/secretGenerator literals live only inside the
    # kustomization file, which we skipped above. kustomize would materialise
    # them into ConfigMap/Secret objects; without it those service-discovery URLs
    # vanish. Synthesise the equivalent objects so the normal resolver sees them.
    gen_docs, gen_sources = _generator_docs(overlay, overlay_ns)
    docs.extend(gen_docs)
    sources.extend(gen_sources)
    return docs, sources


def _generator_docs(
    overlay: Overlay, overlay_ns: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build ConfigMap/Secret objects from the kustomization's generators.

    A ``configMapGenerator`` literal carries plain config (including in-cluster
    URLs we want as edges) so we keep its values. A ``secretGenerator`` literal
    carries secret *values* — we keep ONLY the key name and drop the value, the
    same guarantee SecretView gives for real secrets.
    """
    docs: list[dict[str, Any]] = []
    sources: list[str] = []
    src = str(overlay.kustomization)
    for kdoc in yaml_io.load_all_file(overlay.kustomization):
        for gen in kdoc.get("configMapGenerator") or []:
            data = _literals_to_data(gen.get("literals"), keep_values=True)
            if not gen.get("name") or not data:
                continue
            docs.append(_synthetic("ConfigMap", gen, data, overlay_ns))
            sources.append(src)
        for gen in kdoc.get("secretGenerator") or []:
            data = _literals_to_data(gen.get("literals"), keep_values=False)
            if not gen.get("name") or not data:
                continue
            docs.append(_synthetic("Secret", gen, data, overlay_ns))
            sources.append(src)
    return docs, sources


def _literals_to_data(literals: Any, *, keep_values: bool) -> dict[str, str]:
    data: dict[str, str] = {}
    for item in literals or []:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            continue
        # Never retain a secret value — only its key name.
        data[key] = _unquote(value) if keep_values else ""
    return data


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _synthetic(
    kind: str, gen: dict[str, Any], data: dict[str, str], overlay_ns: str | None
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": str(gen["name"]), "namespace": gen.get("namespace") or overlay_ns},
        "data": data,
    }


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
