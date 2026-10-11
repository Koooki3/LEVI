"""Expected export identity must bind the actual pinned statistics input."""

import copy
import json

import pytest
from fastapi import HTTPException
from test_result_index import change_session, change_state, legacy_client, write_header

from levi.live import api, result_index, stats

SCOPE = {"dataset": "dataset-a", "session": "run-a"}


def binding(identities):
    return json.dumps(
        {"schema": result_index.STATS_BINDING_SCHEMA, "identities": identities}
    )


def selected(client):
    response = client.get("/api/levi/live/stats", params=SCOPE)
    assert response.status_code == 200
    return response.json()["source_identities"]


def root_b(cfg, args, tmp_path):
    other = tmp_path / "root-b"
    write_header(other / "model-a/task-a", "demo_0000", "run-a")
    directory = other / ".eval_sessions"
    directory.mkdir()
    (directory / "same.json").write_bytes(args["session_file"].read_bytes())
    cfg.watch.roots.append(str(other))
    change_state(
        args,
        lambda state: state.update(
            root=str(other), source=str(other / "model-a/task-a")
        ),
    )
    return other


@pytest.mark.parametrize("format", ["json", "csv", "md"])
def test_bound_export_keeps_a_and_rejects_same_name_rebound_b(
    tmp_path, monkeypatch, format
):
    client, cfg, args = legacy_client(tmp_path, monkeypatch)
    expected = selected(client)
    params = {**SCOPE, "format": format, "expected_sources": binding(expected)}
    response = client.get("/api/levi/live/stats/export", params=params)
    assert response.status_code == 200
    assert (
        response.headers["X-LEVI-Stats-Source-Binding"]
        == result_index.STATS_BINDING_SCHEMA
    )
    assert "success" in response.text
    other = root_b(cfg, args, tmp_path)
    rejected = client.get("/api/levi/live/stats/export", params=params)
    assert rejected.status_code == 409
    assert rejected.json() == {
        "detail": {"reason": "statistics_export_source_mismatch"}
    }
    assert str(other) not in rejected.text and "success" not in rejected.text
    compatible = client.get(
        "/api/levi/live/stats/export", params={**SCOPE, "format": format}
    )
    assert compatible.status_code == 200
    assert "X-LEVI-Stats-Source-Binding" not in compatible.headers


@pytest.mark.parametrize("format", ["json", "csv", "md"])
def test_bound_active_legacy_export_preserves_trusted_realtime_statistics(
    tmp_path, monkeypatch, format
):
    client, _, args = legacy_client(tmp_path, monkeypatch)
    change_session(args, state="running")
    expected = selected(client)
    response = client.get(
        "/api/levi/live/stats/export",
        params={**SCOPE, "format": format, "expected_sources": binding(expected)},
    )
    assert response.status_code == 200
    assert (
        response.headers["X-LEVI-Stats-Source-Binding"]
        == result_index.STATS_BINDING_SCHEMA
    )
    assert "success" in response.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("root", "/unknown-root"),
        ("group", "other-group"),
        ("task", "other-task"),
        ("run_id", "other-run"),
        ("session_id", "other-session"),
        ("datasets", ["dataset-a", "unknown-alias"]),
    ],
)
def test_bound_export_requires_complete_exact_identity(
    tmp_path, monkeypatch, field, value
):
    client, _, _ = legacy_client(tmp_path, monkeypatch)
    expected = selected(client)
    expected[0][field] = value
    response = client.get(
        "/api/levi/live/stats/export",
        params={**SCOPE, "expected_sources": binding(expected)},
    )
    assert response.status_code == 409
    assert "success" not in response.text


def test_null_session_and_canonical_alias_dataset_sources(tmp_path, monkeypatch):
    client, cfg, args = legacy_client(tmp_path, monkeypatch)
    change_session(args, session_id=None)
    expected = selected(client)
    assert expected[0]["session_id"] is None
    response = client.get(
        "/api/levi/live/stats/export",
        params={**SCOPE, "expected_sources": binding(expected)},
    )
    assert response.status_code == 200
    state = json.loads((cfg.live_dir / "datasets/dataset-a.json").read_text())
    (cfg.live_dir / "datasets/native-alias.json").write_text(json.dumps(state))
    stats.record(
        cfg.live_dir,
        {
            "dataset": "native-alias",
            "demo": "demo_0000",
            "session": "run-a",
            "at": 2,
            "result": {"state": "done"},
        },
    )
    scoped = {"dataset": "native-alias", "session": "run-a"}
    actual = client.get("/api/levi/live/stats", params=scoped).json()[
        "source_identities"
    ]
    assert actual[0]["datasets"] == ["native-alias"]
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={**scoped, "expected_sources": binding(actual)},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={**scoped, "expected_sources": binding(expected)},
        ).status_code
        == 409
    )
    all_sources = client.get(
        "/api/levi/live/stats", params={"session": "run-a"}
    ).json()["source_identities"]
    assert set(all_sources[0]["datasets"]) == {"dataset-a", "native-alias"}
    reversed_sources = copy.deepcopy(all_sources)
    reversed_sources[0]["datasets"].reverse()
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={"session": "run-a", "expected_sources": binding(reversed_sources)},
        ).status_code
        == 200
    )


def test_real_two_root_sources_bind_as_a_set_and_scope_cannot_shrink(
    tmp_path, monkeypatch
):
    client, cfg, args = legacy_client(tmp_path, monkeypatch)
    path = cfg.live_dir / "datasets/dataset-a.json"
    original = path.read_bytes()
    root_b(cfg, args, tmp_path)
    (cfg.live_dir / "datasets/dataset-b.json").write_bytes(path.read_bytes())
    path.write_bytes(original)
    stats.record(
        cfg.live_dir,
        {
            "dataset": "dataset-b",
            "demo": "demo_0000",
            "session": "run-a",
            "at": 2,
            "result": {"state": "done"},
        },
    )
    response = client.get("/api/levi/live/stats", params={"session": "run-a"})
    assert response.status_code == 200
    actual = response.json()["source_identities"]
    assert len(actual) == 2 and len({x["root"] for x in actual}) == 2
    response = client.get(
        "/api/levi/live/stats/export",
        params={
            "session": "run-a",
            "expected_sources": binding(list(reversed(actual))),
        },
    )
    assert response.status_code == 200
    assert response.json()["source_identities"] == actual
    # The same expected collection cannot be silently narrowed to selected dataset A.
    response = client.get(
        "/api/levi/live/stats/export",
        params={**SCOPE, "expected_sources": binding(actual)},
    )
    assert response.status_code == 409
    # Nor may a global export add B to the previously verified A-only collection.
    response = client.get(
        "/api/levi/live/stats/export",
        params={"session": "run-a", "expected_sources": binding(selected(client))},
    )
    assert response.status_code == 409


def test_unknown_and_mixed_actual_sources_never_get_bound_output(tmp_path, monkeypatch):
    client, _, args = legacy_client(tmp_path, monkeypatch)
    expected = selected(client)
    previous = api.result_index.legacy_sources

    def mixed(*a, **kw):
        actual = previous(*a, **kw)
        actual["source_identities"].append(
            {**actual["source_identities"][0], "root": "/foreign-root"}
        )
        return actual

    monkeypatch.setattr(api.result_index, "legacy_sources", mixed)
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={**SCOPE, "expected_sources": binding(expected)},
        ).status_code
        == 409
    )
    monkeypatch.setattr(api.result_index, "legacy_sources", previous)
    change_state(args, lambda state: state.update(source="/unknown-source"))
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={**SCOPE, "expected_sources": binding(expected)},
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-json",
        "[]",
        "{}",
        '{"schema":"wrong","identities":[]}',
        '{"schema":"levi.live.stats-source-binding.v1","identities":[]}',
        "x" * 8193,
        '"' + "策" * 3000 + '"',
    ],
)
def test_malformed_or_oversized_binding_is_rejected_before_sources(bad, monkeypatch):
    monkeypatch.setattr(
        api,
        "_result_config",
        lambda: pytest.fail("invalid binding reached source loading"),
    )
    with pytest.raises(HTTPException) as error:
        api.stats_export(expected_sources=bad)
    assert error.value.status_code == 400


def test_missing_extra_duplicate_or_wrongly_typed_identity_is_closed(
    tmp_path, monkeypatch
):
    client, _, _ = legacy_client(tmp_path, monkeypatch)
    known = selected(client)
    invalid = [
        known * 2,
        [{**known[0], "datasets": ["dataset-a", "dataset-a"]}],
        [{**known[0], "extra": "unknown"}],
        [{**known[0], "session_id": False}],
    ]
    for field in known[0]:
        missing = copy.deepcopy(known)
        del missing[0][field]
        invalid.append(missing)
    for items in invalid:
        response = client.get(
            "/api/levi/live/stats/export",
            params={**SCOPE, "expected_sources": binding(items)},
        )
        assert response.status_code == 400
    duplicate_key = binding(known).replace(
        '"schema":', '"schema":"duplicate", "schema":'
    )
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={**SCOPE, "expected_sources": duplicate_key},
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={
                **SCOPE,
                "session": "other-run",
                "expected_sources": binding(known),
            },
        ).status_code
        == 409
    )


def test_pinned_source_change_after_bound_aggregation_still_blocks(
    tmp_path, monkeypatch
):
    client, _, args = legacy_client(tmp_path, monkeypatch)
    expected = selected(client)
    previous = api.statsview.build

    def change(*a, **kw):
        result = previous(*a, **kw)
        change_session(args, state="running")
        return result

    monkeypatch.setattr(api.statsview, "build", change)
    assert (
        client.get(
            "/api/levi/live/stats/export",
            params={**SCOPE, "expected_sources": binding(expected)},
        ).status_code
        == 409
    )
