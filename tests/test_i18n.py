"""All three catalogs must stay in sync (no missing translation keys)."""

from kubedian.config import SUPPORTED_LANGS
from kubedian.i18n import catalog, t


def _flatten(d, prefix=""):
    keys = set()
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flatten(v, path)
        else:
            keys.add(path)
    return keys


def test_all_catalogs_have_identical_keys():
    reference = _flatten(catalog("en"))
    for lang in SUPPORTED_LANGS:
        keys = _flatten(catalog(lang))
        assert keys == reference, f"{lang} key mismatch: {reference ^ keys}"


def test_lookup_and_format():
    assert t("es", "edge", "http_calls") == "llama a"
    assert t("pt", "edge", "reads_from") == "lê de"
    assert t("en", "docs", "service_page_title", service="x") == "Service: x"


def test_unknown_lang_falls_back_to_english():
    assert t("de", "edge", "http_calls") == "calls"


def test_unknown_key_returns_path():
    assert t("en", "edge", "does_not_exist") == "does_not_exist"
