-- Kubedian service graph — source of truth.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    namespace  TEXT,
    attrs      TEXT NOT NULL DEFAULT '{}',
    generation INTEGER NOT NULL DEFAULT 0  -- bumped by sync-envs for mark-and-sweep
);

CREATE TABLE IF NOT EXISTS edges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id         TEXT NOT NULL,
    dst_id         TEXT NOT NULL,
    type           TEXT NOT NULL,
    environment    TEXT NOT NULL,
    provenance     TEXT NOT NULL,
    signal         TEXT NOT NULL,
    source_file    TEXT,
    source_locator TEXT,
    confidence     REAL NOT NULL DEFAULT 1.0,
    attrs          TEXT NOT NULL DEFAULT '{}',
    generation     INTEGER NOT NULL DEFAULT 0,  -- bumped by sync-envs for mark-and-sweep
    UNIQUE(src_id, dst_id, type, environment, source_locator)
);

CREATE TABLE IF NOT EXISTS index_meta (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    repo_path         TEXT,
    indexed_at        TEXT,
    kustomize_version TEXT,
    service_count     INTEGER,
    edge_count        INTEGER,
    render_failures   INTEGER,
    generation        INTEGER NOT NULL DEFAULT 0  -- current mark-and-sweep generation
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, environment);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, environment);
CREATE INDEX IF NOT EXISTS idx_edges_env ON edges(environment);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
