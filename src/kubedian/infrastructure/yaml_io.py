"""Safe multi-document YAML parsing (read-only).

Uses PyYAML's ``safe_load_all`` — battle-tested against real Kubernetes
manifests and more forgiving than ruamel for the multi-doc streams that
``kustomize build`` emits. Parsing never raises to the caller: a malformed
document yields nothing rather than aborting the whole index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import yaml


def load_all_text(text: str) -> Iterator[dict[str, Any]]:
    """Yield each mapping document from a multi-doc YAML string."""
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                yield doc
    except Exception:  # noqa: BLE001 - malformed YAML must never abort the index
        return


def load_all_file(path: Path) -> Iterator[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    yield from load_all_text(text)
