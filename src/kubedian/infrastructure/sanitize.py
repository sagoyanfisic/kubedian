"""Guards that ensure secret *values* never reach the graph, vault, or logs.

SOPS keeps key names in plaintext but encrypts values as ``ENC[AES256_GCM,...]``.
We only ever want key *names*. These helpers extract names defensively and a
verification helper asserts a string carries no secret material — used by the
``test_secret_keys_only`` safety test.
"""

from __future__ import annotations

from typing import Any

# Markers that indicate a SOPS-encrypted value slipped through somewhere.
_FORBIDDEN_MARKERS = ("ENC[", "AES256_GCM", "sops:")


def secret_key_names(raw: dict[str, Any]) -> tuple[str, ...]:
    """Return the sorted key names of a Secret's data/stringData, never values."""
    names: set[str] = set()
    for block in ("data", "stringData"):
        section = raw.get(block)
        if isinstance(section, dict):
            names.update(str(k) for k in section.keys())
    return tuple(sorted(names))


def assert_no_secret_values(text: str) -> None:
    """Raise if any encrypted-value marker is present. Used in tests/exporters."""
    for marker in _FORBIDDEN_MARKERS:
        if marker in text:
            raise ValueError(
                f"refusing to emit content containing secret marker {marker!r}"
            )
