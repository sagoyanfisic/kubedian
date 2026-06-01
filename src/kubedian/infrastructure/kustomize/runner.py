"""Read-only ``kustomize build`` wrapper with a safe, sandboxed invocation.

Rendering resolves the namespace transformer, patches and name prefixes/suffixes
so the extractor sees the *actual* deployed objects (critical for the real
an overlay's ``namespace:`` vs a value hardcoded in the base). Helm inflation is
intentionally NOT enabled — helmCharts[] are parsed declaratively elsewhere.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class KustomizeNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    yaml_text: str
    stderr: str


def kustomize_available() -> bool:
    return shutil.which("kustomize") is not None


def kustomize_version() -> str | None:
    if not kustomize_available():
        return None
    try:
        out = subprocess.run(
            ["kustomize", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (out.stdout or out.stderr).strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def render_overlay(overlay_dir: Path, timeout: int = 60) -> RenderResult:
    """Run ``kustomize build`` on an overlay directory. Never raises on build
    failure — returns ``ok=False`` so the caller can fall back to raw YAML."""
    if not kustomize_available():
        raise KustomizeNotFound(
            "kustomize binary not found on PATH. Install kustomize "
            "(https://kustomize.io) to render overlays accurately."
        )
    try:
        proc = subprocess.run(
            ["kustomize", "build", str(overlay_dir)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(overlay_dir),
            # No --enable-exec, no --enable-helm: deterministic and offline.
        )
    except subprocess.TimeoutExpired:
        return RenderResult(ok=False, yaml_text="", stderr="kustomize build timed out")
    except OSError as exc:  # pragma: no cover - environment specific
        return RenderResult(ok=False, yaml_text="", stderr=str(exc))

    if proc.returncode != 0:
        return RenderResult(ok=False, yaml_text="", stderr=proc.stderr.strip())
    return RenderResult(ok=True, yaml_text=proc.stdout, stderr=proc.stderr.strip())
