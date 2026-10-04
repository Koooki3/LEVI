"""Training pool: task text corrections (proposed by anyone, approved by a
person, applied only on request and never to the sources) and the rule for
copies whose texts differ."""

import json
import shutil
from pathlib import Path

import pytest
from test_pool import make_demo

from levi.pool import cli, corrections, export, index, jobs, recipe, scanner
from levi.pool.recipe import Recipe

TOKEN = "test-ui-token"
PERSON = {"x-levi-ui-token": TOKEN}
VERSION = "dir-v1"


def _set_task(demo: Path, text: str) -> None:
    meta = json.loads((demo / "metadata.json").read_text())
    meta["task_description"] = text
    (demo / "metadata.json").write_text(json.dumps(meta))


@pytest.fixture(scope="module")
def corr_root(tmp_path_factory):
    """Two paired tasks, a byte copy whose text differs from its original
    (``fold_cloth/demo_0005 copy``) and a copy-named folder with no original
    whose text is the other task (``unfold_cloth/demo_0009 copy``)."""
    root = tmp_path_factory.mktemp("corr") / "pool"
    data = root / "collect/data"
    for i in range(3):
        make_demo(
            data / "fold_cloth" / f"demo_{i:04d}",
            task="fold_cloth",
            created=f"2026-07-0{i + 1}T10:00:00",
        )
    for i in range(2):
        make_demo(
            data / "unfold_cloth" / f"demo_{i:04d}",
            task="unfold_cloth",
            created=f"2026-07-0{i + 4}T10:00:00",
        )
    copy = data / "unfold_cloth/demo_0005 copy"
    shutil.copytree(data / "fold_cloth/demo_0002", copy)
    _set_task(copy, "unfold_cloth")
    make_demo(
        data / "unfold_cloth/demo_0009 copy",
        task="fold_cloth",
        created="2026-07-09T10:00:00",
    )
    return root


@pytest.fixture
def pool(corr_root, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(corr_root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    scanner.scan()
    return {"root": corr_root, "out": tmp_path}


def _proposals(tmp_path, rows, name="p.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _row(episode, task_from, task_to, id_, **more):
    return {
        "id": id_,
        "episode": episode,
        "task_from": task_from,
        "task_to": task_to,
        "source": "direction-detector",
        "evidence": "first and last frame",
        "confidence": "medium",
        "review_batch": "1",
        "proposed_by": "agent-test",
        **more,
    }


def _import(pool, rows=None, version=VERSION):
    rows = rows or [
        _row("collect/data/fold_cloth/demo_0000", "fold_cloth", "unfold_cloth", "a"),
        _row("collect/data/fold_cloth/demo_0001", "fold_cloth", "unfold_cloth", "b"),
    ]
    return corrections.import_file(_proposals(pool["out"], rows), version)


def _tasks(rec: Recipe) -> dict:
    return {t["task"]: t["episodes"] for t in recipe.preview(rec)["tasks"]}


def _review(client, decision="approved", version=VERSION, **body):
    return client.post(
        f"/api/levi/pool/corrections/{version}/review",
        json={"decision": decision, "reviewer": "Ann", **body},
        headers=PERSON,
    )


@pytest.fixture
def person(client, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    return client


# ------------------------------------------------------------------ import


def test_import_writes_proposals_only_and_never_a_decision(pool):
    made = _import(pool)
    assert made["proposals"] == 2 and len(made["sha256"]) == 64
    shown = corrections.show(VERSION)
    assert shown["counts"] == {"proposed": 2}
    assert shown["match"] == {"ok": 2}
    first = shown["entries"][0]
    assert first["key"].endswith("collect/data/fold_cloth/demo_0000")
    assert first["reviewed_by"] is None and first["proposed_at"]
    # A version is written once.
    with pytest.raises(ValueError, match="written once"):
        _import(pool)
    # An import cannot carry a decision.
    approved = _row("collect/data/fold_cloth/demo_0000", "fold", "unfold", "x")
    for extra in ({"status": "approved"}, {"reviewed_by": "agent"}):
        with pytest.raises(ValueError, match="a person approves"):
            corrections.import_file(
                _proposals(pool["out"], [{**approved, **extra}], "bad.jsonl"), "v2"
            )
    # A proposal that changes nothing, a path with '..', a bad version name.
    with pytest.raises(ValueError, match="does not change"):
        corrections.import_file(
            _proposals(pool["out"], [_row("a/b", "Fold", "fold", "s")], "s.jsonl"),
            "v3",
        )
    with pytest.raises(ValueError, match=r"\.\."):
        corrections.import_file(
            _proposals(pool["out"], [_row("../x", "a", "b", "s")], "d.jsonl"), "v4"
        )
    with pytest.raises(ValueError, match="version name"):
        corrections.import_file(_proposals(pool["out"], [approved], "c.jsonl"), "Bad/")
    assert corrections.versions() == [VERSION]


def test_sources_are_never_written(pool):
    meta = pool["root"] / "collect/data/fold_cloth/demo_0000/metadata.json"
    before = meta.read_bytes()
    _import(pool)
    folder = corrections.folder()
    assert folder.is_relative_to(scanner.settings.pool_dir())
    assert meta.read_bytes() == before


# ------------------------------------------------------------------ review


def test_unreviewed_and_rejected_corrections_are_not_applied(pool, person):
    _import(pool)
    plain = Recipe(name="r", categories=["human"], tasks=["fold cloth", "unfold cloth"])
    asked = plain.model_copy(update={"task_corrections": [VERSION]})
    assert _tasks(plain) == {"fold cloth": 3, "unfold cloth": 2}
    # Proposed only: nothing moves.
    assert _tasks(asked) == {"fold cloth": 3, "unfold cloth": 2}
    assert recipe.preview(asked)["task_corrections"]["applied_groups"] == 0
    # A person approves one and rejects the other.
    assert _review(person, ids=["a"]).json()["changed"] == 1
    assert _review(person, "rejected", ids=["b"]).status_code == 200
    assert _tasks(asked) == {"fold cloth": 2, "unfold cloth": 3}
    # A recipe that does not name the version is unaffected.
    assert _tasks(plain) == {"fold cloth": 3, "unfold cloth": 2}
    # The latest decision counts: approve b after all, then reject a.
    _review(person, ids=["b"])
    _review(person, "rejected", ids=["a"], note="looked again")
    assert _tasks(asked) == {"fold cloth": 2, "unfold cloth": 3}
    entry = {e["id"]: e for e in corrections.show(VERSION)["entries"]}
    assert entry["a"]["status"] == "rejected" and entry["a"]["review_note"]
    assert entry["b"]["status"] == "approved" and entry["b"]["reviewed_by"] == "Ann"
    # Reviewed: the version can no longer be replaced.
    with pytest.raises(ValueError, match="has reviews"):
        corrections.import_file(
            _proposals(pool["out"], [_row("x/y", "a", "b", "z")], "r.jsonl"),
            VERSION,
            replace=True,
        )


def test_review_is_a_persons_action(pool, client, monkeypatch):
    _import(pool)
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    url = f"/api/levi/pool/corrections/{VERSION}/review"
    body = {"decision": "approved", "reviewer": "x", "all": True}
    agent = {"Authorization": "Bearer test-scoped-token"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=agent).status_code == 403
    assert client.post(url, json=body, headers={**agent, **PERSON}).status_code == 403
    assert corrections.show(VERSION)["counts"] == {"proposed": 2}
    # The route refuses again without the service's middleware in front.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.pool.api import router

    bare = FastAPI()
    bare.include_router(router)
    raw = TestClient(bare)
    assert (
        raw.post(url, json=body, headers={"Authorization": "Bearer t"}).status_code
        == 403
    )
    assert raw.post(url, json=body).status_code == 401
    assert corrections.show(VERSION)["counts"] == {"proposed": 2}
    # A person, by batch with one left out.
    done = raw.post(
        url,
        json={**body, "all": False, "batch": "1", "exclude": ["b"]},
        headers=PERSON,
    ).json()
    assert done["changed"] == 1 and done["counts"] == {"approved": 1, "proposed": 1}
    # The reviewer must be named; unknown ids are refused.
    assert (
        raw.post(url, json={**body, "reviewer": " "}, headers=PERSON).status_code >= 400
    )
    assert (
        raw.post(
            url, json={**body, "all": False, "ids": ["nope"]}, headers=PERSON
        ).status_code
        >= 400
    )
    assert (
        client.get("/api/levi/pool/corrections/none-such", headers=PERSON).status_code
        == 404
    )


def test_stale_and_conflicting_corrections(pool, person):
    _import(
        pool,
        [
            # The episode's text is not what the proposal corrects: stale.
            _row("collect/data/fold_cloth/demo_0000", "wave", "unfold_cloth", "s"),
            _row("collect/data/no_such/demo_0000", "fold_cloth", "unfold_cloth", "u"),
            _row(
                "collect/data/fold_cloth/demo_0001", "fold_cloth", "unfold_cloth", "c"
            ),
        ],
    )
    _review(person, all=True)
    rec = Recipe(
        name="r",
        categories=["human"],
        tasks=["fold cloth", "unfold cloth"],
        task_corrections=[VERSION],
    )
    preview = recipe.preview(rec)
    assert {t["task"]: t["episodes"] for t in preview["tasks"]} == {
        "fold cloth": 2,
        "unfold cloth": 3,
    }
    note = next(
        w for w in preview["warnings"] if w["code"] == "task_corrections_not_applied"
    )
    assert note["counts"] == {"stale": 1, "unmatched": 1} and not note["blocking"]
    # A second version that says otherwise about the same recording: blocking.
    _import(
        pool,
        [_row("collect/data/fold_cloth/demo_0001", "fold_cloth", "wave", "w")],
        "dir-v2",
    )
    _review(person, version="dir-v2", all=True)
    both = rec.model_copy(update={"task_corrections": [VERSION, "dir-v2"]})
    clash = next(
        w
        for w in recipe.preview(both)["warnings"]
        if w["code"] == "task_correction_conflict"
    )
    assert clash["blocking"]
    with pytest.raises(ValueError, match="disagree"):
        jobs.plan_export(both, export.ExportOptions(format="raw_capture", name="x"))
    # An unknown version is an error, not a silent no-op.
    with pytest.raises(ValueError, match="No task correction version"):
        recipe.preview(rec.model_copy(update={"task_corrections": ["missing"]}))


# ------------------------------------------------------------------ export


def test_export_applies_approved_corrections_and_records_them(pool, person):
    _import(pool)
    _review(person, ids=["a"])
    rec = Recipe(
        name="r",
        categories=["human"],
        tasks=["unfold cloth"],
        task_corrections=[VERSION],
    )
    options = export.ExportOptions(
        format="raw_capture", name="fixed", output_dir=str(pool["out"] / "exports")
    )
    job = jobs.plan_export(rec, options)
    jobs.execute(job)
    target = pool["out"] / "exports/fixed"
    record = json.loads((target / "pool_export.json").read_text())
    fixed = record["task_corrections"]
    assert fixed["versions"][0]["version"] == VERSION
    assert fixed["episodes_corrected"] == 1
    applied = fixed["applied"][0]
    assert applied["correction"] == f"{VERSION}:a"
    assert applied["reviewed_by"] == "Ann" and applied["task_from"] == "fold_cloth"
    assert len(record["episodes"]) == 3
    corrected = next(e for e in record["episodes"] if e["task_correction"])
    assert corrected["source_path"].endswith("fold_cloth/demo_0000")
    assert corrected["task_original"] == "fold_cloth"
    assert corrected["task"] == "unfold cloth"
    # The exported copy says the corrected task; the source is unchanged.
    meta = json.loads((target / corrected["path"] / "metadata.json").read_text())
    assert meta["task_description"] == "unfold_cloth"
    assert meta["levi_task_correction"] == {
        "correction": f"{VERSION}:a",
        "task_description_original": "fold_cloth",
    }
    source = json.loads(
        (pool["root"] / "collect/data/fold_cloth/demo_0000/metadata.json").read_text()
    )
    assert source["task_description"] == "fold_cloth"
    assert "levi_task_correction" not in source


def test_a_correction_rejected_after_planning_stops_the_export(pool, person):
    _import(pool)
    _review(person, ids=["a"])
    rec = Recipe(
        name="r",
        categories=["human"],
        tasks=["unfold cloth"],
        task_corrections=[VERSION],
    )
    job = jobs.plan_export(
        rec,
        export.ExportOptions(
            format="raw_capture", name="late", output_dir=str(pool["out"] / "exports")
        ),
    )
    _review(person, "rejected", ids=["a"])
    with pytest.raises(ValueError, match="no longer approved"):
        export.run(job)
    assert not (pool["out"] / "exports/late").exists()


# ------------------------------------------------------------------ copies


def test_the_original_is_kept_over_a_copy_and_differing_texts_are_listed(pool):
    every = index.episodes(limit=1000, show_copies=True)["episodes"]
    copy = next(r for r in every if r["episode"] == "unfold_cloth/demo_0005 copy")
    assert not copy["canonical"]
    assert copy["canonical_key"].endswith("fold_cloth/demo_0002")
    found = {c["kind"]: c for c in corrections.copy_candidates()}
    group = found["group_text_differs"]
    assert group["kept"].endswith("fold_cloth/demo_0002")
    assert group["kept_task"] == "fold cloth"
    assert {m["task_raw"] for m in group["members"]} == {"fold_cloth", "unfold_cloth"}
    lone = found["nonstandard_text_differs"]
    assert lone["key"].endswith("unfold_cloth/demo_0009 copy")
    assert lone["task_raw"] == "fold_cloth" and lone["folder_task"] == "unfold cloth"
    # A recipe that picks the original is told, not silently given one text.
    preview = recipe.preview(
        Recipe(name="r", categories=["human"], tasks=["fold cloth"])
    )
    warning = next(w for w in preview["warnings"] if w["code"] == "copy_task_conflict")
    assert not warning["blocking"] and warning["count"] == 1
    assert warning["episodes"][0].endswith("fold_cloth/demo_0002")


def test_an_approved_correction_settles_a_copy_conflict(pool, person):
    _import(
        pool,
        [_row("collect/data/fold_cloth/demo_0002", "fold_cloth", "unfold_cloth", "k")],
    )
    _review(person, ids=["k"])
    rec = Recipe(
        name="r",
        categories=["human"],
        tasks=["fold cloth", "unfold cloth"],
        task_corrections=[VERSION],
    )
    preview = recipe.preview(rec)
    assert {w["code"] for w in preview["warnings"]}.isdisjoint({"copy_task_conflict"})
    # The whole recording (original and copy) moved; one episode counts.
    assert {t["task"]: t["episodes"] for t in preview["tasks"]} == {
        "fold cloth": 2,
        "unfold cloth": 3,
    }


# ------------------------------------------------------------------ CLI and recipes


def test_cli_import_list_show_and_review_through_the_service(
    pool, person, monkeypatch, capsys
):
    rows = [
        _row("collect/data/fold_cloth/demo_0000", "fold_cloth", "unfold_cloth", "a")
    ]
    path = _proposals(pool["out"], rows)
    assert cli.main(["corrections", "import", str(path), "--version", VERSION]) == 0
    assert cli.main(["corrections", "list"]) == 0
    capsys.readouterr()
    assert cli.main(["corrections", "show", VERSION, "--status", "proposed"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"proposed": 1}
    from levi.agent import core

    # No running service: the review is refused, nothing is recorded.
    monkeypatch.setattr(core, "status", lambda: None)
    assert (
        cli.main(["corrections", "approve", VERSION, "--all", "--reviewer", "Ann"]) == 1
    )
    assert "not running" in capsys.readouterr().out
    assert corrections.show(VERSION)["counts"] == {"proposed": 1}
    # With the service, the CLI sends the person's decision to the review route.
    sent = []

    def request(path, payload=None, *, human=False, **_):
        sent.append((path, human))
        return person.post(path, json=payload, headers=PERSON).json()

    monkeypatch.setattr(core, "status", lambda: {"instance": "x"})
    monkeypatch.setattr(core, "request", request)
    assert (
        cli.main(["corrections", "approve", VERSION, "--id", "a", "--reviewer", "Ann"])
        == 0
    )
    assert sent == [(f"/api/levi/pool/corrections/{VERSION}/review", True)]
    assert corrections.show(VERSION)["counts"] == {"approved": 1}
    assert cli.main(["corrections", "copies"]) == 0


def test_recipes_keep_their_correction_versions(pool):
    saved = recipe.save(Recipe(name="keep", task_corrections=[VERSION, VERSION]))
    assert saved
    assert recipe.load("keep").task_corrections == [VERSION]
    with pytest.raises(ValueError):
        Recipe(name="bad", task_corrections=["Not A Version"])
