"""Kubedian — service-level dependency graph from Kubernetes/Kustomize manifests."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kubedian")
except PackageNotFoundError:  # running from source without install
    __version__ = "0.0.0+dev"

__all__ = ["__version__"]
