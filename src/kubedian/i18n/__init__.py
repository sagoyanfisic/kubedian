"""Trilingual string catalogs (en/es/pt) for generated docs and CLI messages."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib.resources import files

from kubedian.config import DEFAULT_LANG, SUPPORTED_LANGS


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict:
    lang = lang.lower()
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG
    data = files("kubedian.i18n").joinpath(f"{lang}.toml").read_bytes()
    return tomllib.loads(data.decode("utf-8"))


def t(lang: str, *path: str, **fmt) -> str:
    """Look up a dotted key path in the catalog; format with kwargs."""
    node: object = catalog(lang)
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:  # fall back to English, then the raw key
            node = catalog(DEFAULT_LANG)
            for k in path:
                node = node.get(k, k) if isinstance(node, dict) else k
            break
    if isinstance(node, str) and fmt:
        return node.format(**fmt)
    return node if isinstance(node, str) else ".".join(path)
