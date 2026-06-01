# Kubedian — notes for AI agents

Kubedian is a service-level dependency graph for Kubernetes/Kustomize repos.
**It is complementary to codegraph, not a replacement:**

- **codegraph** answers questions *within* a repo: symbols, call graphs, impact of
  changing a function. It explicitly does NOT model relationships between services.
- **Kubedian** answers questions *between* services: who calls a service over HTTP,
  what databases/caches/queues/external APIs it depends on, request paths, and blast
  radius. It reads deploy-time topology from manifests, not code call paths.

Use both: codegraph inside a service repo, Kubedian across the manifests repo.

## Architecture (read-only pipeline, mirrors codegraph's stages)

`discover` overlays → `render` (kustomize, raw-YAML fallback) → `extract` typed
resources → `resolve` edges with provenance → `store` in SQLite → serve via MCP /
export to Obsidian vault / Mermaid / trilingual docs.

Source of truth is **SQLite** (`.kubedian/graph.db`). Every other surface reads it.

## Hard rules

- **Never decrypt or store secret values.** Only secret *key names* are read
  (`SecretView` exposes `.key_names` only; `sanitize.assert_no_secret_values` guards
  every exporter; `test_secret_keys_only` enforces it). SOPS values stay `ENC[...]`.
- **Provenance is first-class.** Edges are `explicit` (ConfigMap/manifest states it,
  confidence 1.0) or `heuristic` (inferred from a key name like `POSTGRES_HOST`,
  confidence < 1.0). Never present a heuristic edge as a hard fact — cite
  `source_file` / `source_locator`.
- **The overlay's `namespace:` is authoritative** (kustomize's namespace transformer).
  Trust it over namespaces hardcoded in base/patches.
- **One broken overlay must never abort the index** — fall back to raw YAML and record
  `render_mode`; surface failures in `index_status()`.

## Layout

- `domain/` — entities (graph, resources) and repository interfaces.
- `application/pipeline/` — discover/render/extract/resolve/index.
- `application/heuristics/` — env-key rules + DNS parsing.
- `infrastructure/` — kustomize runner, sqlite store/reader, vault/mermaid/docs exporters.
- `presentation/tools/` — FastMCP tool registrations.
- `i18n/` — en/es/pt string catalogs (keys must stay in sync; `test_i18n` enforces).

## Commands

```bash
uv sync --extra mcp
uv run pytest -q
uv run kubedian index --repo <repo> --env staging
uv run kubedian export-vault --db .kubedian/graph.db --out ./vault
uv run kubedian export-mermaid --db .kubedian/graph.db --focus <svc> --lang es
uv run kubedian serve --db .kubedian/graph.db      # MCP over stdio
```
