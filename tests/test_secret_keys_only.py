"""Safety: secret VALUES must never reach the graph/vault/logs."""

import pytest

from kubedian.infrastructure.sanitize import assert_no_secret_values, secret_key_names

# A SOPS-encrypted Secret as it appears on disk: plaintext keys, ENC[...] values.
SOPS_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "orders-service", "namespace": "orders"},
    "stringData": {
        "POSTGRES_HOST": "ENC[AES256_GCM,data:abc==,tag:xyz==]",
        "AUTH_SERVICE_URL": "ENC[AES256_GCM,data:def==,tag:uvw==]",
        "RABBITMQ_PASSWORD": "ENC[AES256_GCM,data:ghi==,tag:rst==]",
    },
    "sops": {"encrypted_regex": "^(data|stringData)$"},
}


def test_secret_key_names_returns_only_names():
    names = secret_key_names(SOPS_SECRET)
    assert set(names) == {"POSTGRES_HOST", "AUTH_SERVICE_URL", "RABBITMQ_PASSWORD"}
    # no value material anywhere in the returned names
    for name in names:
        assert "ENC[" not in name
        assert "AES256_GCM" not in name


def test_assert_no_secret_values_detects_leak():
    assert_no_secret_values("POSTGRES_HOST=postgres reads_from db")  # clean → ok
    with pytest.raises(ValueError):
        assert_no_secret_values("value: ENC[AES256_GCM,data:abc==]")


def test_full_index_db_carries_no_secret_values(tmp_path):
    """End-to-end: nothing written to SQLite contains an encrypted marker."""
    from pathlib import Path

    from kubedian.application.pipeline.index import index_repo
    from kubedian.domain.entities.graph import Environment
    from tests.conftest import write_sample_repo

    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    blob = Path(db).read_bytes().decode("latin-1")
    assert "ENC[" not in blob
    assert "AES256_GCM" not in blob
