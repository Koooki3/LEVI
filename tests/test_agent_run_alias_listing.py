"""Canonical human run queries retain the original external-agent grants."""

import pytest
from test_links import LINKED, NAME, make_live

from levi import links, paths, service
from levi.agent.capabilities import invoke
from levi.agent.runtime import Workbench
from levi.agent.security import Principal

NATIVE = "local/" + NAME
ALIAS = "local/" + LINKED
HUMAN = Principal("operator", human=True)


def add_run(wb, identifier, repo, *, key=NAME, created=1, status="planned"):
    record = {
        "id": identifier,
        "dataset_key": key,
        "created_at": created,
        "status": status,
        "completed": [],
        "context": {
            "repo_id": repo,
            "workflow": {"kind": "temporal"},
            "episodes": [0],
            "cameras": [],
            "instruction": "Review motion",
        },
        "provider_config": {"name": "external"},
    }
    wb.store.put("runs", identifier, record)
    return record


@pytest.fixture
def linked_live(client, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path / "product")
    paths.ROOT.mkdir()
    workspace = make_live(tmp_path / "livews")
    monkeypatch.setenv(links.ENV, str(workspace))
    return workspace


@pytest.fixture
def alias_runs(client, linked_live):
    product = Workbench(service.STATE)
    canonical = Workbench(linked_live / links.STATE)
    records = [
        add_run(canonical, "native-run", NATIVE, created=1),
        add_run(canonical, "product-run", ALIAS, created=2),
    ]
    return product, canonical, records


def listed(wb, repo=None, principal=HUMAN, **kwargs):
    arguments = {**kwargs, **({"repo_id": repo} if repo else {})}
    return invoke(wb, principal, "runs.list", arguments)["runs"]


@pytest.mark.parametrize("product_query", [True, False])
def test_human_alias_and_native_queries_find_both_historical_contexts(
    alias_runs, product_query
):
    product, canonical, records = alias_runs
    wb, repo = (product, ALIAS) if product_query else (canonical, NATIVE)
    rows = listed(wb, repo)
    assert [(row["run_id"], row["dataset"]) for row in rows] == [
        ("product-run", ALIAS),
        ("native-run", NATIVE),
    ]
    assert [canonical.store.get("runs", record["id"]) for record in records] == records
    assert product.store.list("runs") == []


def test_same_name_in_other_stores_is_never_merged(
    alias_runs, linked_live, tmp_path, monkeypatch
):
    product, canonical, _ = alias_runs
    other = make_live(tmp_path / "other")
    foreign = Workbench(other / links.STATE)
    add_run(foreign, "foreign-run", NATIVE)
    add_run(product, "own-run", NATIVE)
    monkeypatch.setenv(links.ENV, f"{linked_live},{other}")
    other_alias = next(
        name for name, link in links.links().items() if link.workspace == other
    )
    assert {row["run_id"] for row in listed(product, ALIAS)} == {
        "native-run",
        "product-run",
    }
    assert [row["run_id"] for row in listed(product, "local/" + other_alias)] == [
        "foreign-run"
    ]
    assert [row["run_id"] for row in listed(product, NATIVE)] == ["own-run"]
    assert len(canonical.store.list("runs")) == 2


def test_local_matching_requires_the_persisted_dataset_key(alias_runs):
    product, canonical, _ = alias_runs
    add_run(canonical, "wrong-key", ALIAS, key="different")
    assert {row["run_id"] for row in listed(product, ALIAS)} == {
        "native-run",
        "product-run",
    }


def test_remote_repo_ids_do_not_match_local_keys_or_similar_names(alias_runs):
    product, canonical, _ = alias_runs
    remote = "organization/" + NAME
    add_run(canonical, "remote-run", remote)
    add_run(canonical, "other-remote", "organization/live." + NAME)
    assert {row["run_id"] for row in listed(product, ALIAS)} == {
        "native-run",
        "product-run",
    }
    assert [row["run_id"] for row in listed(canonical, remote)] == ["remote-run"]


@pytest.mark.parametrize(
    "datasets, expected",
    [
        ((ALIAS,), {"product-run"}),
        ((NATIVE, ALIAS), {"native-run", "product-run"}),
    ],
)
def test_external_alias_query_keeps_original_record_grants(
    alias_runs, datasets, expected
):
    product, _, _ = alias_runs
    # An alias-only grant does not cover a historical native context.
    principal = Principal("connection", datasets=datasets)
    assert {row["run_id"] for row in listed(product, ALIAS, principal)} == expected


@pytest.mark.parametrize(
    "datasets, operations",
    [
        ((), ("read",)),
        ((NATIVE,), ("read",)),
        ((ALIAS,), ("draft",)),
    ],
)
def test_missing_dataset_or_operation_grant_still_refuses(
    alias_runs, datasets, operations
):
    product, _, _ = alias_runs
    principal = Principal("connection", datasets=datasets, operations=operations)
    with pytest.raises(PermissionError):
        listed(product, ALIAS, principal)


def test_alias_rebinding_never_brings_old_store_records_into_new_store(
    alias_runs, tmp_path, monkeypatch
):
    product, canonical, _ = alias_runs
    other = make_live(tmp_path / "rebound")
    foreign = Workbench(other / links.STATE)
    add_run(foreign, "new-native", NATIVE)
    add_run(foreign, "new-alias", ALIAS)
    monkeypatch.setenv(links.ENV, str(other))
    assert {row["run_id"] for row in listed(product, ALIAS)} == {
        "new-native",
        "new-alias",
    }
    # The old alias context no longer resolves to this native Store.
    assert [row["run_id"] for row in listed(canonical, NATIVE)] == ["native-run"]


def test_unfiltered_permissions_finished_and_limit_keep_existing_behavior(alias_runs):
    _, canonical, _ = alias_runs
    add_run(canonical, "finished", NATIVE, created=3, status="cancelled")
    assert [row["run_id"] for row in listed(canonical, limit=1)] == ["product-run"]
    assert [
        row["run_id"] for row in listed(canonical, include_finished=True, limit=1)
    ] == ["finished"]
    agent = Principal("native-connection", datasets=(NATIVE,))
    assert [row["run_id"] for row in listed(canonical, principal=agent)] == [
        "native-run"
    ]


@pytest.fixture
def broken_historical_alias(alias_runs, linked_live, tmp_path, monkeypatch):
    product, canonical, _ = alias_runs
    foreign = make_live(tmp_path / "broken-foreign", name="other-source")
    monkeypatch.setenv(links.ENV, f"{linked_live},{foreign}")
    alias = "local/live.other-source"
    add_run(canonical, "unrelated-historical", alias, key="other-source", created=99)
    view = foreign / links.STATE / "views" / "other-source"
    (foreign / links.STATE / "agent").symlink_to(view, target_is_directory=True)
    # Exercise the real linked-source guard, not a mocked resolver exception.
    with pytest.raises(ValueError, match="symbolic links"):
        links.target(alias.split("/", 1)[1], canonical.store.state)
    return product, canonical, alias


@pytest.mark.parametrize("grant", ["human", "native-only", "native-and-unrelated"])
def test_broken_historical_alias_cannot_abort_healthy_query(
    broken_historical_alias, monkeypatch, grant
):
    _, canonical, alias = broken_historical_alias
    principal = (
        HUMAN
        if grant == "human"
        else Principal(
            "connection",
            datasets=(NATIVE, alias) if grant == "native-and-unrelated" else (NATIVE,),
        )
    )
    resolved = []
    real_target = links.target

    def track_target(name, own=None):
        resolved.append(name)
        return real_target(name, own)

    monkeypatch.setattr(links, "target", track_target)
    rows = listed(canonical, NATIVE, principal)
    expected = ["product-run", "native-run"] if grant == "human" else ["native-run"]
    assert [row["run_id"] for row in rows] == expected
    if grant == "native-only":
        assert alias.split("/", 1)[1] not in resolved
    else:
        assert alias.split("/", 1)[1] in resolved


def test_broken_requested_alias_is_still_refused(broken_historical_alias):
    product, _, alias = broken_historical_alias
    with pytest.raises(ValueError, match="symbolic links"):
        listed(product, alias)
