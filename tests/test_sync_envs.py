"""`sync-envs` must refresh the secret/env subset (names + file only), sweep what
vanished, stay idempotent, and never write a secret value."""

import json
import sqlite3
from pathlib import Path

from kubedian.application.pipeline.index import index_repo
from kubedian.application.pipeline.sync import sync_envs
from kubedian.domain.entities.graph import Environment
from kubedian.infrastructure.sqlite import graph_store
from tests.conftest import write_sample_repo

_SECRET_ID = "secret:ns-a/service-a-secret"
_SECRET_FILE = "service-a/overlays/staging/secrets.yaml"


def _secret_keys(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT attrs FROM nodes WHERE id = ?", (_SECRET_ID,)).fetchone()
    finally:
        conn.close()
    return set(json.loads(row[0]).get("keys", [])) if row else set()


def _edge_locators(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT source_locator FROM edges WHERE src_id = 'svc:ns-a/service-a'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _rewrite_secret(repo: Path, keys: list[str]) -> None:
    """Rewrite service-a's SOPS secret with a new set of (encrypted-value) keys."""
    body = "\n".join(f"  {k}: ENC[AES256_GCM,data:zz==,tag:yy==]" for k in keys)
    (repo / _SECRET_FILE).write_text(
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: service-a-secret\n"
        "  namespace: ns-a\n"
        "stringData:\n"
        f"{body}\n"
        "sops:\n"
        "  encrypted_regex: ^(data|stringData)$\n",
        encoding="utf-8",
    )


def test_sync_envs_adds_and_sweeps_keys(tmp_path):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)

    assert _secret_keys(db) == {
        "POSTGRES_HOST", "REDIS_URI", "RABBITMQ_HOST", "RABBITMQ_PORT", "EMAIL_API_URL",
    }
    assert graph_store.current_generation(db) == 1

    # Drop EMAIL_API_URL + REDIS_URI, add NEW_TOKEN.
    _rewrite_secret(repo, ["POSTGRES_HOST", "RABBITMQ_HOST", "RABBITMQ_PORT", "NEW_TOKEN"])
    stats = sync_envs(repo, db, Environment.STAGING)

    assert stats["generation"] == 2
    assert _secret_keys(db) == {"POSTGRES_HOST", "RABBITMQ_HOST", "RABBITMQ_PORT", "NEW_TOKEN"}
    # the env-derived edge keyed on the removed REDIS_URI must be swept
    locators = _edge_locators(db)
    assert "NEW_TOKEN" in locators or "NEW_TOKEN" in _secret_keys(db)
    assert "REDIS_URI" not in locators
    assert stats["edges_swept"] >= 1


def test_sync_envs_is_idempotent(tmp_path):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)

    first = sync_envs(repo, db, Environment.STAGING)
    second = sync_envs(repo, db, Environment.STAGING)

    # nothing changed on disk → second run must sweep nothing
    assert second["generation"] == first["generation"] + 1
    assert second["nodes_swept"] == 0
    assert second["edges_swept"] == 0


def test_sync_envs_leaves_services_untouched(tmp_path):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)

    def _count(node_type: str) -> int:
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(
                "SELECT count(*) FROM nodes WHERE type = ?", (node_type,)
            ).fetchone()[0]
        finally:
            conn.close()

    before = _count("service")
    sync_envs(repo, db, Environment.STAGING)
    assert _count("service") == before


def test_sync_envs_never_writes_secret_values(tmp_path):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    sync_envs(repo, db, Environment.STAGING)

    blob = Path(db).read_bytes().decode("latin-1")
    assert "ENC[" not in blob
    assert "AES256_GCM" not in blob


def test_sync_envs_requires_existing_index(tmp_path):
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "missing.db"
    try:
        sync_envs(repo, db, Environment.STAGING)
    except FileNotFoundError as exc:
        assert "kubedian index" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected FileNotFoundError when no index exists")
