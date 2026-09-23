"""Namespaces: one input dataset, many isolated experiments. A namespace
``<dataset>--<name>`` reads the dataset's single source folder but keeps its
own products, so nothing one experiment commits reaches another's model
context and the input is never copied."""

import json

import pytest

from levi import catalog


def atoms(content):
    return [{"role": "user", "content": content, "style": "subtask", "timestamp": 0.0}]


@pytest.fixture
def base(client, dataset):
    return client.post("/api/levi/catalog", json={"path": str(dataset)}).json()


def test_a_namespace_is_one_more_name_over_the_same_source(base, dataset):
    item = catalog.create_namespace(base["name"], "r9-agentCode")
    assert item["name"] == f"{base['name']}--r9-agentCode"
    assert item["id"] == "local/" + item["name"]
    assert item["path"] == str(dataset) and item["base"] == base["name"]
    # Idempotent; path lookups still answer the dataset itself.
    assert catalog.create_namespace(base["name"], "r9-agentCode") == item
    assert catalog.name_for_path(dataset) == base["name"]
    assert catalog.local_root(item["id"]) == catalog.local_root(base["id"])
    assert catalog.display_name(item["id"], str(dataset)) == item["name"]
    assert [n["name"] for n in catalog.namespaces(base["name"])] == [item["name"]]


@pytest.mark.parametrize("bad", ["", "a--b", "-a", "a b", "x" * 65, "../x"])
def test_namespace_names_are_checked(base, bad):
    with pytest.raises(ValueError):
        catalog.create_namespace(base["name"], bad)


def test_no_namespace_of_an_unknown_dataset_or_of_a_namespace(base):
    with pytest.raises(ValueError, match="not registered"):
        catalog.create_namespace("missing", "a")
    ns = catalog.create_namespace(base["name"], "a")
    with pytest.raises(ValueError, match="namespaces of its own"):
        catalog.create_namespace(ns["name"], "b")


def test_annotations_never_cross_namespaces(client, base, tmp_path):
    a = catalog.create_namespace(base["name"], "exp-a")["id"]
    b = catalog.create_namespace(base["name"], "exp-b")["id"]
    for repo, text in ((a, "from a"), (base["id"], "from base")):
        result = client.post(
            "/annotations/api/episodes/0/atoms",
            json={"repo_id": repo, "episode_index": 0, "atoms": atoms(text)},
        )
        assert result.status_code == 200, result.text

    def seen(repo):
        rows = client.get(
            "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
        ).json()["atoms"]
        return [r["content"] for r in rows]

    assert seen(a) == ["from a"]
    assert seen(base["id"]) == ["from base"]
    assert seen(b) == []
    sidecars = tmp_path / "outputs" / "annotations"
    assert {p.name for p in sidecars.iterdir()} >= {
        base["name"],
        f"{base['name']}--exp-a",
    }
    # Exports are per namespace too.
    out = client.post("/annotations/api/export", json={"repo_id": a}).json()
    assert f"{base['name']}--exp-a_annotated" in out["output_dir"]


def test_a_namespace_follows_its_base_and_goes_with_it(client, base, tmp_path):
    from levi.sync import Synchronizer

    ns = catalog.create_namespace(base["name"], "exp")
    catalog.add_entry(
        catalog.inside(base["path"]), {"revision": "changed-by-a-rebuild"}
    )
    assert catalog.datasets()[ns["name"]]["revision"] == "changed-by-a-rebuild"
    catalog.remove_entry(base["name"])
    changes = Synchronizer(root=tmp_path).scan()
    assert ("removed", ns["name"]) in [(c["kind"], c.get("name")) for c in changes]
    assert ns["name"] not in catalog.datasets()


def test_namespace_notes_never_become_shared_knowledge(base, tmp_path):
    from levi.harness import knowledge

    ns = catalog.create_namespace(base["name"], "exp")
    memory = catalog.STATE / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    note = {"teaching": [{"note": "A note only this experiment taught.", "count": 3}]}
    (memory / f"{ns['name']}.json").write_text(json.dumps(note))
    (memory / f"{base['name']}.json").write_text(
        json.dumps({"teaching": [{"note": "A note the dataset taught.", "count": 3}]})
    )
    texts = [row[1] for row in knowledge._sources(catalog.STATE)]
    assert texts == ["A note the dataset taught."]


def test_namespace_api_and_cli(client, base, capsys):
    made = client.post(
        f"/api/levi/catalog/{base['name']}/namespaces", json={"namespace": "exp"}
    )
    assert made.status_code == 200, made.text
    listed = client.get(f"/api/levi/catalog/{base['name']}/namespaces").json()
    assert [n["name"] for n in listed["namespaces"]] == [made.json()["name"]]
    assert (
        client.post(
            f"/api/levi/catalog/{base['name']}/namespaces", json={"namespace": "a--b"}
        ).status_code
        == 400
    )
    from levi.cli import namespace_cli

    assert namespace_cli(["list", base["name"]]) == 0
    assert capsys.readouterr().out.split() == [made.json()["name"]]
    assert namespace_cli(["remove", base["name"]]) == 1  # not a namespace
    assert namespace_cli(["remove", made.json()["name"]]) == 0
    assert made.json()["name"] not in catalog.datasets()
