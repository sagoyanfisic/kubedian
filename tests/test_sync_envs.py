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


def _service_a_secret_edge_attrs(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT attrs FROM edges WHERE src_id = 'svc:ns-a/service-a' "
            "AND dst_id = ? AND type = 'references'",
            (_SECRET_ID,),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else {}


def test_sync_envs_sweeps_valuefrom_env_map(tmp_path):
    """Removing the valueFrom block from the deployment must drop the var->key
    mapping (and the env_key_ref mode) from the REFERENCES edge on sync."""
    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)

    attrs = _service_a_secret_edge_attrs(db)
    assert attrs.get("env_map") == {"DATABASE_HOST": "POSTGRES_HOST"}
    assert "env_key_ref" in attrs.get("modes", [])

    dep = repo / "service-a/base/deployment.yaml"
    text = dep.read_text(encoding="utf-8")
    start = text.index("          env:")
    end = text.index("          envFrom:")
    dep.write_text(text[:start] + text[end:], encoding="utf-8")

    sync_envs(repo, db, Environment.STAGING)
    attrs = _service_a_secret_edge_attrs(db)
    assert "env_map" not in attrs
    assert attrs.get("modes") == ["env_from"]


def test_sync_locator_representative_change_leaves_no_duplicate_rows(tmp_path):
    """Edge.key excludes source_locator while SQLite's UNIQUE includes it. When
    the representative locator of a merged edge changes between syncs (here: the
    reads_from edge keyed on POSTGRES_HOST becomes keyed on DB_HOST), the old row
    must be swept in the same transaction — never left as a duplicate."""
    from tests.conftest import repo_writer

    repo = tmp_path / "r"
    w = repo_writer(repo)
    w(
        "billing/base/deployment.yaml",
        """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: billing
          namespace: placeholder
          labels: {app: billing}
        spec:
          template:
            metadata:
              labels: {app: billing}
            spec:
              containers:
                - name: api
                  image: x:1
                  envFrom:
                    - secretRef: {name: billing-secret}
        """,
    )
    w("billing/overlays/staging/kustomization.yaml", "namespace: billing\nresources:\n  - ../../base\n  - secrets.yaml\n")

    def _write_secret(key: str) -> None:
        w(
            "billing/overlays/staging/secrets.yaml",
            f"""
            apiVersion: v1
            kind: Secret
            metadata:
              name: billing-secret
              namespace: billing
            stringData:
              {key}: ENC[AES256_GCM,data:zz==,tag:yy==]
            sops:
              encrypted_regex: ^(data|stringData)$
            """,
        )

    def _reads_from_rows() -> list[str]:
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT source_locator FROM edges WHERE src_id = 'svc:billing/billing' "
                "AND type = 'reads_from'"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    _write_secret("POSTGRES_HOST")
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING)
    assert _reads_from_rows() == ["POSTGRES_HOST"]

    # Same postgres heuristic, different key name -> same logical edge, new locator.
    _write_secret("DB_HOST")
    sync_envs(repo, db, Environment.STAGING)
    assert _reads_from_rows() == ["DB_HOST"]  # exactly one row, new locator
