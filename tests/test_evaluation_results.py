import hashlib
import json

import pytest

from levi.evaluation_results import CatalogError, entries, register


def setup_report(tmp_path, *, progress=False):
    report = tmp_path / "reports/c001/operator_label"
    report.mkdir(parents=True)
    artifacts = (
        {"progress.json": b'{"valid": 0}'}
        if progress
        else {
            "data/analysis.json": b'{"n": 2}',
            "figures/success.svg": b"<svg/>",
        }
    )
    for name, raw in artifacts.items():
        path = report / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {
        "schema": "levi.aeri.campaign_report_manifest.v1",
        "campaign_id": "c001",
        "basis": "operator_label",
        "campaign_state": "RUNNING" if progress else "REPORTED",
        "progress_only": progress,
        "files": {
            name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            for name, raw in artifacts.items()
        },
        "arms": [{"secret_mapping": "should-not-appear"}],
    }
    (report / "manifest.json").write_text(json.dumps(manifest))
    raw_root = tmp_path / "rollouts/models"
    source = raw_root / "recap_r2_best_step14300_jax/stack_the_plates"
    source.mkdir(parents=True)
    return {
        "root": tmp_path / "catalog",
        "report": report,
        "rollout_root": raw_root,
        "rollout_dir": source,
        "model": source.parent.name,
        "task": source.name,
        "session": "eval-20261011-001",
    }


def test_register_idempotent_and_source_untouched(tmp_path):
    args = setup_report(tmp_path)
    before = {str(p): p.read_bytes() for p in args["report"].rglob("*") if p.is_file()}
    first = register(**args)
    assert register(**args) == first
    assert entries(args["root"]) == [first]
    assert len(list(args["root"].glob("models/*/*/sessions/*/versions/*.json"))) == 1
    assert (args["root"] / "campaigns/c001/operator_label/reference.json").is_file()
    assert before == {
        str(p): p.read_bytes() for p in args["report"].rglob("*") if p.is_file()
    }
    assert "figures/success.svg" in first["report"]["files"]
    assert "recap_r2_best_step14300_jax" in (args["root"] / "INDEX.md").read_text()


def test_progress_does_not_publish_arm_metadata(tmp_path):
    args = setup_report(tmp_path, progress=True)
    row = register(**args)
    assert row["progress_only"]
    assert list(row["report"]["files"]) == ["progress.json"]
    assert entries(args["root"]) == []
    assert "model" not in row and "session" not in row
    assert "secret_mapping" not in json.dumps(row)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "../escape"),
        ("task", "a/b"),
        ("session", "."),
        ("session", "x|html"),
    ],
)
def test_bad_identity_no_catalog(tmp_path, field, value):
    args = setup_report(tmp_path)
    args[field] = value
    with pytest.raises(CatalogError):
        register(**args)
    assert not args["root"].exists()


def test_mismatched_model_source_rejected(tmp_path):
    args = setup_report(tmp_path)
    args["model"] = "wrong-model"
    with pytest.raises(CatalogError):
        register(**args)
    assert not args["root"].exists()


def test_hash_mismatch_no_partial_reference(tmp_path):
    args = setup_report(tmp_path)
    (args["report"] / "data/analysis.json").write_text("changed")
    with pytest.raises(CatalogError):
        register(**args)
    assert not args["root"].exists()


@pytest.mark.parametrize("attack", ["artifact", "catalog", "rollout"])
def test_symlink_refused(tmp_path, attack):
    args = setup_report(tmp_path)
    if attack == "artifact":
        artifact = args["report"] / "data/analysis.json"
        raw = artifact.read_bytes()
        artifact.unlink()
        other = tmp_path / "outside.json"
        other.write_bytes(raw)
        artifact.symlink_to(other)
    elif attack == "catalog":
        other = tmp_path / "other"
        other.mkdir()
        args["root"].symlink_to(other, target_is_directory=True)
    else:
        source = args["rollout_dir"]
        source.rmdir()
        source.symlink_to(args["report"], target_is_directory=True)
    with pytest.raises(CatalogError):
        register(**args)


def test_artifact_traversal_refused(tmp_path):
    args = setup_report(tmp_path)
    p = args["report"] / "manifest.json"
    value = json.loads(p.read_text())
    value["files"] = {"../secret.json": {"sha256": "a" * 64}}
    p.write_text(json.dumps(value))
    with pytest.raises(CatalogError):
        register(**args)


def test_concurrent_sessions_preserve_index(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    args = setup_report(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(lambda s: register(**{**args, "session": s}), ["eval-a", "eval-b"])
        )
    assert {r["session"] for r in entries(args["root"])} == {"eval-a", "eval-b"}
    index = json.loads((args["root"] / "index.json").read_text())
    assert len(index["entries"]) == 2


def test_analysis_cannot_be_reblinded_under_same_identity(tmp_path):
    args = setup_report(tmp_path)
    register(**args)
    p = args["report"] / "manifest.json"
    manifest = json.loads(p.read_text())
    manifest.update(progress_only=True, campaign_state="RUNNING", files={})
    p.write_text(json.dumps(manifest))
    with pytest.raises(CatalogError, match="re-blinded"):
        register(**args)
    assert len(entries(args["root"])) == 1


def test_progress_index_has_campaign_entry_without_model(tmp_path):
    args = setup_report(tmp_path, progress=True)
    register(**args)
    index = (args["root"] / "INDEX.md").read_text()
    assert "c001 / operator_label" in index
    assert args["model"] not in index


def test_cli_lists_catalog(tmp_path, capsys):
    from levi.evaluation_results import main

    args = setup_report(tmp_path)
    register(**args)
    assert main(["list", "--root", str(args["root"])]) == 0
    assert json.loads(capsys.readouterr().out)[0]["model"] == args["model"]
