"""Kubedian command-line interface."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

import typer

import json as _json

from kubedian import __version__
from kubedian.application.pipeline.index import index_repo
from kubedian.application.pipeline.sync import sync_envs
from kubedian.application.use_cases import queries
from kubedian.config import DEFAULT_LANG, SUPPORTED_LANGS, default_db_path
from kubedian.domain.entities.graph import Environment
from kubedian.infrastructure.kustomize.runner import kustomize_available
from kubedian.infrastructure.sqlite.graph_reader import GraphReader

app = typer.Typer(
    name="kubedian",
    help="Service-level dependency graph from Kubernetes/Kustomize manifests.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kubedian {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    _version: Optional[bool] = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Kubedian — like Obsidian + codegraph, but for microservices."""


@app.command()
def index(
    path: Optional[Path] = typer.Argument(None, help="Manifests repo to index (default: current directory)."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Manifests repo (alias for the positional path)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help="Limit to one environment (development|staging|production|test)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Output SQLite path (default: <repo>/.kubedian/graph.db)."),
    docs: bool = typer.Option(False, "--docs", help="Also ingest human-authored docs (Mermaid diagrams) as 'documented' edges, from <repo>/docs."),
    docs_dir: Optional[Path] = typer.Option(None, "--docs-dir", help="Explicit docs directory to ingest (implies --docs)."),
) -> None:
    """Index a manifests repo into a SQLite service graph (defaults to the current directory)."""
    target = repo or path or Path.cwd()
    if not target.is_dir():
        raise typer.BadParameter(f"{target} is not a directory")
    if not kustomize_available():
        typer.secho(
            "⚠  kustomize not found on PATH — falling back to raw YAML parsing "
            "(namespaces/patches may be inaccurate). Install kustomize for best results.",
            fg=typer.colors.YELLOW,
        )
    environment = _parse_env(env)
    db_path = db or default_db_path(target)

    docs_path: Optional[Path] = None
    if docs_dir is not None:
        docs_path = docs_dir
    elif docs:
        docs_path = target / "docs"
    if docs_path is not None and not docs_path.is_dir():
        raise typer.BadParameter(f"docs directory not found: {docs_path}")

    report = _index_with_progress(target, db_path, environment, docs_path)
    _print_summary(report, environment)


@app.command(name="sync-envs")
def sync_envs_cmd(
    path: Optional[Path] = typer.Argument(None, help="Manifests repo (default: current directory)."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Manifests repo (alias for the positional path)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help="Limit to one environment."),
    db: Optional[Path] = typer.Option(None, "--db", help="SQLite path (default: <repo>/.kubedian/graph.db)."),
) -> None:
    """Refresh only the secret/env subset (key names + file locations, never values).

    Re-reads the manifests and mark-and-sweeps the secret/configmap nodes and
    env-derived edges: new keys are added, vanished ones are deleted, and the rest
    of the graph (services, jobs, real call edges) is left untouched. Requires an
    existing index — run `kubedian index` first.
    """
    target = repo or path or Path.cwd()
    if not target.is_dir():
        raise typer.BadParameter(f"{target} is not a directory")
    environment = _parse_env(env)
    db_path = db or default_db_path(target)
    if not db_path.exists():
        raise typer.BadParameter(f"no index at {db_path} — run `kubedian index` first")

    stats = _sync_with_progress(target, db_path, environment)
    typer.secho(
        f"✓ sync-envs (generation {stats['generation']})", fg=typer.colors.GREEN, bold=True
    )
    typer.echo(
        f"  nodes: +{stats['nodes_written']} written, -{stats['nodes_swept']} swept · "
        f"edges: +{stats['edges_written']} written, -{stats['edges_swept']} swept"
    )


def _sync_with_progress(target: Path, db_path: Path, environment) -> dict:
    try:
        from rich.progress import (
            BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
        )
    except ImportError:  # pragma: no cover - rich ships with typer
        return sync_envs(target, db_path, environment)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[svc]}"),
        transient=False,
    ) as progress:
        task = progress.add_task("Syncing envs", total=None, svc="discovering…")

        def hook(done: int, total: int, svc: str) -> None:
            progress.update(task, total=total, completed=done, svc=svc)

        return sync_envs(target, db_path, environment, on_overlay=hook)


def _index_with_progress(target: Path, db_path: Path, environment, docs_path: Optional[Path] = None):
    """Run the index with a rich progress bar (falls back to plain if rich absent)."""
    try:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )
    except ImportError:  # pragma: no cover - rich ships with typer
        return index_repo(target, db_path, environment, docs_dir=docs_path)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[svc]}"),
        transient=False,
    ) as progress:
        task = progress.add_task("Rendering overlays", total=None, svc="discovering…")

        def hook(done: int, total: int, svc: str) -> None:
            progress.update(task, total=total, completed=done, svc=svc)

        return index_repo(target, db_path, environment, on_overlay=hook, docs_dir=docs_path)


def _resolve_db(db: Optional[Path], repo: Optional[Path]) -> Path:
    if db is not None:
        return db
    if repo is not None:
        return default_db_path(repo)
    candidate = default_db_path(Path.cwd())
    return candidate


def _open_reader(db: Optional[Path], repo: Optional[Path]) -> GraphReader:
    db_path = _resolve_db(db, repo)
    try:
        return GraphReader(db_path)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc))


def _check_lang(lang: str) -> str:
    if lang not in SUPPORTED_LANGS and lang != "all":
        valid = ", ".join((*SUPPORTED_LANGS, "all"))
        raise typer.BadParameter(f"unknown language {lang!r}. Valid: {valid}")
    return lang


@app.command(name="export-vault")
def export_vault_cmd(
    out: Path = typer.Option(Path("./vault"), "--out", "-o", help="Output vault directory."),
    db: Optional[Path] = typer.Option(None, "--db", help="Path to graph.db."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repo (to locate its .kubedian/graph.db)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help="Environment filter."),
    lang: str = typer.Option(DEFAULT_LANG, "--lang", "-l", help="en|es|pt."),
) -> None:
    """Export an Obsidian vault (one note per service, [[wikilinks]] per edge)."""
    _check_lang(lang)
    reader = _open_reader(db, repo)
    from kubedian.infrastructure.vault.obsidian_exporter import export_vault

    graph = reader.graph(_parse_env(env))
    n = export_vault(graph, out, lang=lang)
    reader.close()
    typer.secho(f"✓ Wrote {n} notes to {out}", fg=typer.colors.GREEN)
    typer.echo(f"  Open {out} as a vault in Obsidian to explore the graph.")


@app.command(name="export-mermaid")
def export_mermaid_cmd(
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output .md/.mmd file (default: stdout)."),
    focus: Optional[str] = typer.Option(None, "--focus", "-f", help="Focus on one service (name or node id)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Path to graph.db."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repo (to locate its .kubedian/graph.db)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help="Environment filter."),
    lang: str = typer.Option(DEFAULT_LANG, "--lang", "-l", help="en|es|pt."),
    fenced: bool = typer.Option(True, "--fenced/--raw", help="Wrap in a ```mermaid block."),
    include_isolated: bool = typer.Option(
        False, "--include-isolated", help="Also draw edge-less nodes (e.g. a CronJob with no deps)."
    ),
) -> None:
    """Generate a MermaidJS architecture diagram (full topology or focused)."""
    _check_lang(lang)
    reader = _open_reader(db, repo)
    from kubedian.infrastructure.mermaid import mermaid_renderer

    graph = reader.graph(_parse_env(env))
    if focus:
        node_id = _find_node_id(reader, focus)
        diagram = mermaid_renderer.render_focus(graph, node_id, lang=lang)
    else:
        diagram = mermaid_renderer.render_flowchart(graph, lang=lang, include_isolated=include_isolated)
    reader.close()

    body = f"```mermaid\n{diagram.rstrip()}\n```\n" if fenced else diagram
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        typer.secho(f"✓ Wrote diagram to {out}", fg=typer.colors.GREEN)
    else:
        typer.echo(body)


@app.command(name="export-docs")
def export_docs_cmd(
    out: Path = typer.Option(Path("./kubedian-docs"), "--out", "-o", help="Output docs directory."),
    db: Optional[Path] = typer.Option(None, "--db", help="Path to graph.db."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repo (to locate its .kubedian/graph.db)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help="Environment filter."),
    lang: str = typer.Option("all", "--lang", "-l", help="en|es|pt|all."),
) -> None:
    """Generate trilingual architecture docs (Markdown + embedded Mermaid)."""
    _check_lang(lang)
    reader = _open_reader(db, repo)
    from kubedian.infrastructure.docs.docs_generator import export_docs

    graph = reader.graph(_parse_env(env))
    reader.close()
    langs = SUPPORTED_LANGS if lang == "all" else (lang,)
    total = 0
    for lg in langs:
        target = (out / lg) if lang == "all" else out
        n = export_docs(graph, target, lang=lg)
        total += n
        typer.secho(f"✓ {lg}: {n} pages → {target}", fg=typer.colors.GREEN)
    typer.echo(f"  {total} pages total.")


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(_json.dumps(result, indent=2, ensure_ascii=False))
        return
    if "error" in result:
        typer.secho(f"✗ {result['error']}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(_json.dumps(result, indent=2, ensure_ascii=False, default=str))


@app.command()
def status(
    db: Optional[Path] = typer.Option(None, "--db", help="Path to graph.db."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repo (to locate its .kubedian/graph.db)."),
    json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show index status and statistics."""
    reader = _open_reader(db, repo)
    _emit(queries.status(reader), json)
    reader.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="Substring of a service / datastore / external name."),
    limit: int = typer.Option(25, "--limit", "-n"),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Restrict to one namespace."),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Search for services / datastores / external APIs by name."""
    reader = _open_reader(db, repo)
    _emit(queries.search(reader, query, limit, namespace=namespace), json)
    reader.close()


@app.command()
def context(
    service: str = typer.Argument(..., help="Service name or node id."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Full context for a service: identity (kind/replicas/ports/SA), who calls it, what it calls, datastores, externals, routing, storage, identity, autoscaling."""
    reader = _open_reader(db, repo)
    _emit(queries.context(reader, service, _parse_env(env)), json)
    reader.close()


@app.command()
def callers(
    service: str = typer.Argument(...),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Find all services that call a service (incoming dependencies)."""
    reader = _open_reader(db, repo)
    _emit(queries.callers(reader, service, _parse_env(env)), json)
    reader.close()


@app.command()
def callees(
    service: str = typer.Argument(...),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Find everything a service depends on (services, db, cache, queue, external, storage/PVC, service account)."""
    reader = _open_reader(db, repo)
    _emit(queries.callees(reader, service, _parse_env(env)), json)
    reader.close()


@app.command()
def trace(
    source: str = typer.Argument(..., help="Source service."),
    target: str = typer.Argument(..., help="Target service."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    max_depth: int = typer.Option(6, "--max-depth"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Find a request path between two services (transitive http_calls + Istio/Ingress routes, so paths through a gateway are traced)."""
    reader = _open_reader(db, repo)
    _emit(queries.trace(reader, source, target, _parse_env(env), max_depth), json)
    reader.close()


@app.command()
def impact(
    service: str = typer.Argument(...),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    max_depth: int = typer.Option(5, "--max-depth"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Analyze the blast radius: services transitively affected if this one fails."""
    reader = _open_reader(db, repo)
    _emit(queries.impact(reader, service, _parse_env(env), max_depth), json)
    reader.close()


@app.command()
def composition(
    service: str = typer.Argument(..., help="Service name or node id."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Full technical composition of a service in one call: identity (containers with roles/resources/probes, ports, env var names), bundle siblings, config (key NAMES only — never values), storage, autoscaling, ServiceAccount + RBAC roles, NetworkPolicy connectivity (both directions) and routing exposure."""
    reader = _open_reader(db, repo)
    _emit(queries.service_composition(reader, service, _parse_env(env)), json)
    reader.close()


@app.command()
def namespace(
    namespace: str = typer.Argument(..., help="Namespace name."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Everything living in a namespace grouped by type, plus its cross-namespace relations (edges in/out aggregated by peer namespace and edge type)."""
    reader = _open_reader(db, repo)
    _emit(queries.namespace_contents(reader, namespace, _parse_env(env)), json)
    reader.close()


@app.command()
def secrets(
    service: str = typer.Argument(..., help="Service name or node id."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Secrets and ConfigMaps a service consumes — key NAMES only (never values), with consumption mode and var→key mapping."""
    reader = _open_reader(db, repo)
    _emit(queries.service_secrets(reader, service, _parse_env(env)), json)
    reader.close()


@app.command()
def ports(
    service: str = typer.Argument(..., help="Service name or node id."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Every port fact for a service: containerPorts, the Service's port→targetPort wiring, and Ingress/VirtualService exposure."""
    reader = _open_reader(db, repo)
    _emit(queries.service_ports(reader, service, _parse_env(env)), json)
    reader.close()


@app.command(name="find-key")
def find_key_cmd(
    query: str = typer.Argument(..., help="Env var or secret/configmap key name (e.g. POSTGRES_HOST)."),
    partial: bool = typer.Option(False, "--partial", help="Substring match instead of exact (case-insensitive either way)."),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Restrict to workloads in one namespace."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Reverse lookup: which workloads use an env var / secret key NAME, from which Secret/ConfigMap (names only, never values)."""
    reader = _open_reader(db, repo)
    _emit(queries.find_key_usage(reader, query, _parse_env(env), partial=partial, namespace=namespace), json)
    reader.close()


@app.command(name="find-port")
def find_port_cmd(
    port: int = typer.Argument(..., help="Port number (e.g. 8000)."),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Restrict to one namespace."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Reverse lookup: which services listen on a port and which Ingress/VirtualService routes target it."""
    reader = _open_reader(db, repo)
    _emit(queries.find_port(reader, port, _parse_env(env), namespace=namespace), json)
    reader.close()


@app.command(name="datastore-clients")
def datastore_clients_cmd(
    datastore: str = typer.Argument(..., help="Datastore node id or name."),
    env: Optional[str] = typer.Option(None, "--env", "-e"),
    db: Optional[Path] = typer.Option(None, "--db"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """List every service that reads/writes/queues/caches a given datastore."""
    reader = _open_reader(db, repo)
    _emit(queries.datastore_clients(reader, datastore, _parse_env(env)), json)
    reader.close()


@app.command()
def install(
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Manifests repo (its .kubedian/graph.db is used)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Path to graph.db."),
    name: str = typer.Option("kubedian", "--name", help="MCP server name to register."),
) -> None:
    """Install the kubedian MCP server into Claude Code (via `claude mcp add`)."""
    import shutil
    import subprocess

    db_path = _resolve_db(db, repo)
    mcp_bin = _mcp_binary()
    if shutil.which("claude") is None:
        typer.secho("`claude` CLI not found. Add this MCP server manually:", fg=typer.colors.YELLOW)
        typer.echo(_manual_mcp_config(name, db_path, mcp_bin))
        raise typer.Exit(1)
    # Use the absolute path: the agent spawns the command in an env whose PATH
    # may not include ~/.local/bin, so a bare `kubedian-mcp` would fail to start.
    cmd = ["claude", "mcp", "add", name, "--env", f"KUBEDIAN_DB={db_path}", "--", mcp_bin]
    typer.echo("$ " + " ".join(cmd))
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        typer.secho(f"✓ Registered MCP server '{name}' in Claude Code.", fg=typer.colors.GREEN)
    else:
        typer.secho("Registration failed. Manual config:", fg=typer.colors.YELLOW)
        typer.echo(_manual_mcp_config(name, db_path))
    raise typer.Exit(rc)


@app.command()
def uninstall(name: str = typer.Option("kubedian", "--name", help="MCP server name to remove.")) -> None:
    """Remove the kubedian MCP server from Claude Code (via `claude mcp remove`)."""
    import shutil
    import subprocess

    if shutil.which("claude") is None:
        raise typer.BadParameter("`claude` CLI not found.")
    rc = subprocess.run(["claude", "mcp", "remove", name]).returncode
    raise typer.Exit(rc)


def _mcp_binary() -> str:
    """Absolute path to the kubedian-mcp executable (PATH-independent)."""
    import shutil
    import sys

    found = shutil.which("kubedian-mcp")
    if found:
        return found
    sibling = Path(sys.argv[0]).resolve().parent / "kubedian-mcp"
    return str(sibling) if sibling.exists() else "kubedian-mcp"


def _manual_mcp_config(name: str, db_path: Path, mcp_bin: str = "kubedian-mcp") -> str:
    return _json.dumps(
        {"mcpServers": {name: {"command": mcp_bin, "env": {"KUBEDIAN_DB": str(db_path)}}}},
        indent=2,
    )


@app.command()
def serve(
    db: Optional[Path] = typer.Option(None, "--db", help="Path to graph.db."),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repo (to locate its .kubedian/graph.db)."),
    http: bool = typer.Option(False, "--http", help="Serve over HTTP instead of stdio."),
) -> None:
    """Run the MCP server (stdio by default) so agents can query the topology."""
    import os

    os.environ["KUBEDIAN_DB"] = str(_resolve_db(db, repo))
    if http:
        os.environ["KUBEDIAN_MCP_TRANSPORT"] = "http"
    try:
        from kubedian.main import main as mcp_main
    except ModuleNotFoundError:
        raise typer.BadParameter(
            "MCP extra not installed. Install with: pip install 'kubedian[mcp]'"
        )
    mcp_main()


def _find_node_id(reader: GraphReader, focus: str) -> str:
    if reader.node(focus):
        return focus
    matches = [n for n in reader.search(focus, limit=50) if n.type.value == "service"]
    if not matches:
        raise typer.BadParameter(f"no service matching {focus!r}")
    return matches[0].id


def _parse_env(env: Optional[str]) -> Optional[Environment]:
    if env is None:
        return None
    try:
        return Environment(env.lower())
    except ValueError:
        valid = ", ".join(e.value for e in Environment)
        raise typer.BadParameter(f"unknown environment {env!r}. Valid: {valid}")


_NODE_STYLE = {
    "service": "bold cyan",
    "namespace": "blue",
    "database": "green",
    "cache": "red",
    "queue": "yellow",
    "external_api": "magenta",
    "helm_chart": "bright_blue",
}
_NODE_ICON = {
    "service": "◉", "namespace": "▤", "database": "🛢", "cache": "⚡",
    "queue": "✉", "external_api": "🌐", "helm_chart": "⎈", "ingress_host": "🔗",
}
_EDGE_STYLE = {
    "http_calls": "cyan", "reads_from": "green", "writes_to": "green",
    "caches_in": "red", "queues_to": "yellow", "calls_external": "magenta",
    "authenticates_via": "blue", "depends_on_chart": "bright_blue",
    "routes_to": "blue", "in_namespace": "dim",
}


def _print_summary(report, environment) -> None:
    graph = report.graph
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:  # pragma: no cover
        return _print_summary_plain(report, environment)

    console = Console()
    scope = environment.value if environment else "all environments"

    if graph is None:
        console.print(f"[green]✓ Indexed[/] {report.repo}")
        return

    node_types = Counter(n.type.value for n in graph.nodes.values())
    edge_types = Counter(e.type.value for e in graph.edges)
    prov = Counter(e.provenance.value for e in graph.edges)

    # header panel
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right")
    meta.add_column(style="bold")
    meta.add_row("Repo", str(report.repo))
    meta.add_row("DB", str(report.db_path))
    meta.add_row("Scope", scope)
    meta.add_row("Overlays", str(report.overlays_total))
    meta.add_row("Services", f"[cyan]{node_types.get('service', 0)}[/]")
    console.print(Panel(meta, title="[bold green]✓ Kubedian index[/]", border_style="green", expand=False))

    # nodes + edges side by side
    nodes_tbl = Table(title=f"Nodes ({len(graph.nodes)})", title_style="bold", box=None, pad_edge=False)
    nodes_tbl.add_column("Type"); nodes_tbl.add_column("Count", justify="right")
    for typ, n in node_types.most_common():
        icon = _NODE_ICON.get(typ, "•")
        nodes_tbl.add_row(f"[{_NODE_STYLE.get(typ, 'white')}]{icon} {typ}[/]", str(n))

    edges_tbl = Table(title=f"Edges ({len(graph.edges)})", title_style="bold", box=None, pad_edge=False)
    edges_tbl.add_column("Type"); edges_tbl.add_column("Count", justify="right")
    for typ, n in edge_types.most_common():
        edges_tbl.add_row(f"[{_EDGE_STYLE.get(typ, 'white')}]{typ}[/]", str(n))

    from rich.columns import Columns
    console.print(Columns([nodes_tbl, edges_tbl], padding=(0, 4)))

    # provenance bar
    total = max(sum(prov.values()), 1)
    exp, heu, doc = prov.get("explicit", 0), prov.get("heuristic", 0), prov.get("documented", 0)
    width = 40
    exp_w = round(width * exp / total)
    doc_w = round(width * doc / total)
    heu_w = max(width - exp_w - doc_w, 0)
    bar = f"[green]{'█' * exp_w}[/][blue]{'█' * doc_w}[/][yellow]{'█' * heu_w}[/]"
    extra = f" · [blue]documented {doc}[/]" if doc else ""
    console.print(
        f"\n  Provenance  {bar}  "
        f"[green]explicit {exp}[/] · [yellow]heuristic {heu}[/]{extra}"
    )

    if report.render_failures:
        shown = ", ".join(report.failed_overlays[:8])
        extra = f" [dim]+{len(report.failed_overlays) - 8} more[/]" if len(report.failed_overlays) > 8 else ""
        console.print(
            f"\n[yellow]⚠ {report.render_failures}/{report.overlays_total} overlay(s) used raw-YAML "
            f"fallback[/] [dim](kustomize build failed — often the ksops generator):[/]\n  [dim]{shown}{extra}[/]"
        )


def _print_summary_plain(report, environment) -> None:
    graph = report.graph
    typer.secho(f"\n✓ Indexed {report.repo}", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  DB:       {report.db_path}")
    typer.echo(f"  Scope:    {environment.value if environment else 'all environments'}")
    typer.echo(f"  Overlays: {report.overlays_total}")
    if graph is None:
        return
    typer.echo(f"  Nodes:    {len(graph.nodes)}")
    typer.echo(f"  Edges:    {len(graph.edges)}")
    if report.render_failures:
        typer.secho(f"  ⚠ {report.render_failures} overlay(s) used raw-YAML fallback", fg=typer.colors.YELLOW)


if __name__ == "__main__":
    app()
