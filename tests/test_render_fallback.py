"""The index must survive overlays that don't build (raw-YAML fallback)."""


from kubedian.application.pipeline.discover import discover_overlays
from kubedian.application.pipeline.extract import extract_overlay
from kubedian.domain.entities.graph import Environment
from kubedian.domain.entities.resource import RenderMode
from tests.conftest import write_sample_repo


def test_unbuildable_overlay_falls_back_and_keeps_namespace(tmp_path):
    repo = write_sample_repo(tmp_path)
    # An overlay that references a non-existent base → kustomize build fails.
    broken = repo / "broken-svc/overlays/staging/kustomization.yaml"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text(
        "namespace: ns-broken\nresources:\n  - ../../base\n  - does-not-exist.yaml\n",
        encoding="utf-8",
    )
    (repo / "broken-svc/base/deployment.yaml").parent.mkdir(parents=True, exist_ok=True)
    (repo / "broken-svc/base/deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: broken-svc\n"
        "  namespace: wrong-ns\nspec:\n  template:\n    spec:\n      containers:\n"
        "        - name: api\n          image: x:1\n",
        encoding="utf-8",
    )

    overlays = {o.service: o for o in discover_overlays(repo, Environment.STAGING)}
    result = extract_overlay(overlays["broken-svc"])

    assert result.render_mode == RenderMode.RAW_FALLBACK
    assert result.render_error
    # overlay namespace wins over the hardcoded wrong-ns
    assert result.overlay_namespace == "ns-broken"
    dep = next(r for r in result.resources if r.kind == "Deployment")
    assert dep.namespace == "ns-broken"


def test_index_does_not_crash_on_mixed_repo(tmp_path):
    from kubedian.application.pipeline.index import index_repo

    repo = write_sample_repo(tmp_path)
    db = tmp_path / "graph.db"
    report = index_repo(repo, db, Environment.STAGING)
    assert db.exists()
    assert report.graph is not None
    assert report.overlays_total >= 3
