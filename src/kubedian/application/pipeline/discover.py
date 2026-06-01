"""Stage 0 — discover the renderable units (overlay kustomizations) in a repo.

A repo following the standard ``<service>/overlays/<env>/kustomization.yaml``
layout exposes one renderable overlay per (service, environment). We locate
every overlay directory that contains a kustomization file and tag it with its
environment (inferred from the directory name).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kubedian.domain.entities.graph import Environment

_KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")

_ENV_ALIASES = {
    "development": Environment.DEVELOPMENT,
    "dev": Environment.DEVELOPMENT,
    "staging": Environment.STAGING,
    "stage": Environment.STAGING,
    "production": Environment.PRODUCTION,
    "prod": Environment.PRODUCTION,
    "test": Environment.TEST,
}


@dataclass(frozen=True)
class Overlay:
    service: str
    environment: Environment
    directory: Path
    kustomization: Path


def discover_overlays(repo: Path, environment: Environment | None = None) -> list[Overlay]:
    """Find overlay kustomizations under ``<service>/overlays/<env>/``.

    If ``environment`` is given, only overlays for that environment are returned.
    """
    repo = repo.resolve()
    overlays: list[Overlay] = []
    for kustomization in _iter_kustomizations(repo):
        directory = kustomization.parent
        env = _infer_environment(directory)
        if env is None:
            continue
        if environment is not None and env != environment:
            continue
        service = _infer_service(directory, repo)
        overlays.append(
            Overlay(
                service=service,
                environment=env,
                directory=directory,
                kustomization=kustomization,
            )
        )
    overlays.sort(key=lambda o: (o.service, o.environment.value))
    return overlays


def _iter_kustomizations(repo: Path):
    for path in repo.rglob("*"):
        if path.name in _KUSTOMIZATION_NAMES and path.is_file():
            # only overlays live under an ".../overlays/<env>/" path
            parts = {p.name for p in path.parents}
            if "overlays" in parts:
                yield path


def _infer_environment(directory: Path) -> Environment | None:
    return _ENV_ALIASES.get(directory.name.lower())


def _infer_service(overlay_dir: Path, repo: Path) -> str:
    """The service name is the path segment immediately before ``overlays``."""
    for parent in overlay_dir.parents:
        if parent.parent is not None and parent.name == "overlays":
            return parent.parent.name
    # Fallback: first path segment under the repo root.
    rel = overlay_dir.relative_to(repo)
    return rel.parts[0] if rel.parts else overlay_dir.name
