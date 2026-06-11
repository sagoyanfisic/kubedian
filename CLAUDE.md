# Kubedian — notes for AI agents

Kubedian is a service-level dependency graph for Kubernetes/Kustomize repos.
**It is complementary to codegraph, not a replacement:**

- **codegraph** answers questions *within* a repo: symbols, call graphs, impact of
  changing a function. It explicitly does NOT model relationships between services.
- **Kubedian** answers questions *between* services: who calls a service over HTTP,
  what databases/caches/queues/external APIs it depends on, request paths, blast
  radius, plus the structural infra around each service (PVCs, HPAs, ServiceAccounts,
  Istio gateways, NetworkPolicy connectivity). It reads deploy-time topology from
  manifests, not code call paths.

Use both: codegraph inside a service repo, Kubedian across the manifests repo.

## Graph model

**One node per workload, not per overlay.** A directory that bundles `api` +
`celery-worker` + `celery-beat` + `celery-flower` yields a node *each*, so a
sub-workload's PVC/HPA/ingress attach to the right node. The **workload name is the
authoritative alias** (Services, HPA `scaleTargetRef` and Ingress backends reference
workloads by name); a `app` label *shared* across a bundle never overrides a name
alias, and an ambiguous shared-label selector resolves to nothing rather than guessing.

Node types (`domain/entities/graph.py`): `service` (Deployment/StatefulSet/DaemonSet),
`job` / `cronjob` (batch — excluded from service-only queries so migrations/backups
don't pollute them), `database`/`cache`/`queue`, `external_api`, `configmap`/`secret`,
`ingress_host`, `gateway`, `storage` (PVC), `autoscaler` (HPA), `service_account`,
`helm_chart`, `namespace`.

Edge types: `http_calls`, `routes_to` (Istio VirtualService / Ingress / Gateway),
`reads_from`/`writes_to`/`caches_in`/`queues_to`, `calls_external`, `allows_to`
(NetworkPolicy — *permitted* connectivity, never a call: excluded from `trace`),
`mounts` (→PVC), `scales` (HPA→workload), `runs_as` (→ServiceAccount),
`depends_on_chart`, `references`, `owns`, `in_namespace`. `trace` follows `http_calls`
**and** `routes_to` (so paths through a gateway are traced).

**Config is queryable.** A `references` edge records *how* a Secret/ConfigMap is
consumed in `attrs`: `modes` (`env_from` / `env_key_ref` / `volume_mount`), `keys`
(the key names consumed) and `env_map` (env var → key for `valueFrom` wiring) —
names only, never values. Workload nodes carry `ports`/`named_ports`/`env_vars`;
their `port_map` lists each fronting Service's `port → target_port` wiring (named
targetPorts resolved); `routes_to` edges carry the Ingress/VirtualService destination
`port`. Reverse lookups: `find_key_usage` (who uses env var/key X) and `find_port`.

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
  confidence 1.0) or `heuristic` (inferred, confidence < 1.0). Never present a heuristic
  edge as a hard fact — cite `source_file` / `source_locator`.
- **A shared service-discovery catalog is not a call.** A discovery ConfigMap consumed
  by ≥2 services that lists ≥3 targets is a *catalog*: having a target's URL available
  ≠ calling it, so its `http_calls` edges are downgraded to `heuristic` (flagged
  `shared_catalog`). A configmap specific to one service stays `explicit`. If an
  independent explicit signal (DNS literal) exists for the same edge, dedup keeps it.
- **`configMapGenerator`/`secretGenerator` are synthesized in raw-fallback** (they live
  only in `kustomization.yaml`, which the fallback skips). Generator values are matched
  by value (cluster DNS → edge), name-agnostic; `secretGenerator` contributes **key
  names only**, never values.
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
uv run kubedian secrets <svc> --json           # Secrets/ConfigMaps + key names + modes (never values)
uv run kubedian ports <svc> --json             # containerPorts + Service port→targetPort + Ingress/VS exposure
uv run kubedian find-key POSTGRES_HOST --json  # reverse lookup by env var / key name (--partial for substring)
uv run kubedian find-port 8000 --json          # reverse lookup by port
uv run kubedian export-vault --db .kubedian/graph.db --out ./vault
uv run kubedian export-mermaid --db .kubedian/graph.db --focus <svc> --lang es
uv run kubedian export-mermaid --db .kubedian/graph.db --include-isolated  # draw edge-less nodes too
uv run kubedian serve --db .kubedian/graph.db      # MCP over stdio
```
