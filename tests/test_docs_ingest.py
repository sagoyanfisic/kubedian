"""The --docs ingestion adds 'documented' edges from Mermaid diagrams."""

from pathlib import Path
from textwrap import dedent

from kubedian.application.pipeline.index import index_repo
from kubedian.domain.entities.graph import EdgeType, Environment, Provenance
from kubedian.infrastructure.sqlite.graph_reader import GraphReader
from tests.conftest import write_sample_repo


def _write_docs(repo: Path) -> Path:
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "architecture.md").write_text(
        dedent(
            """
            # Architecture

            ```mermaid
            flowchart LR
                A["service-a"]
                B["service-b"]
                Z["External ERP"]
                MQ["RabbitMQ\\nCloudAMQP"]
                A -->|HTTP| Z
                A -->|publish events| MQ
                A -->|HTTP| B
            ```
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return docs


def test_docs_add_documented_edges(tmp_path):
    repo = write_sample_repo(tmp_path)
    docs = _write_docs(repo)
    db = tmp_path / "graph.db"

    report = index_repo(repo, db, Environment.STAGING, docs_dir=docs)
    assert report.documented_edges > 0

    reader = GraphReader(db)
    out = reader.callees("svc:ns-a/service-a", Environment.STAGING)

    # service-a → External ERP (external, documented)
    documented = [e for e in out if e.provenance == Provenance.DOCUMENTED]
    assert documented, "expected at least one documented edge"
    assert any("external-erp" in e.dst_id.lower() for e in documented)
    # the RabbitMQ doc node is classified as a queue
    assert any(e.type == EdgeType.QUEUES_TO and "rabbitmq" in e.dst_id.lower() for e in out)
    reader.close()


def test_docs_do_not_invent_services(tmp_path):
    """Unmatched doc labels must not become new 'service' nodes."""
    repo = write_sample_repo(tmp_path)
    docs = _write_docs(repo)
    db = tmp_path / "graph.db"
    index_repo(repo, db, Environment.STAGING, docs_dir=docs)

    reader = GraphReader(db)
    service_names = {n.name for n in reader.nodes() if n.type.value == "service"}
    assert "External ERP" not in service_names  # external, not a service
    reader.close()
