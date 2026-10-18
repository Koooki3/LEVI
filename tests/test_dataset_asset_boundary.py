"""Dataset assets cannot expose private run sidecars; temporary files only."""

import pytest

PRIVATE_MARKER = b'{"private_evidence":"DO_NOT_STREAM"}'
PUBLIC = b'{"asset":true}'


@pytest.fixture
def asset_route(client, tmp_path, monkeypatch):
    from levi import service

    root = tmp_path / "dataset"
    for directory in ("meta", "data", "videos", "images"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(service, "local_root", lambda repo: root)
    return client, root, service


def assert_private(client, path):
    for method, headers in (("GET", {}), ("HEAD", {}), ("GET", {"Range": "bytes=0-3"})):
        response = client.request(
            method, "/api/levi/files/test/" + path, headers=headers
        )
        assert response.status_code == 403
        assert b"DO_NOT_STREAM" not in response.content


@pytest.mark.parametrize(
    "path",
    [
        "meta/%2e%2e/.aeri/runs/r/evidence.json",
        "meta/%2e/info.json",
        "meta/nested/%2e%2e/info.json",
        "meta/.aeri/runs/r/evidence.json",
    ],
)
def test_raw_dot_components_and_nested_private_runs_are_refused(asset_route, path):
    client, root, _ = asset_route
    private = root / ".aeri/runs/r/evidence.json"
    private.parent.mkdir(parents=True)
    private.write_bytes(PRIVATE_MARKER)
    nested = root / "meta/.aeri/runs/r/evidence.json"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(PRIVATE_MARKER)
    (root / "meta/info.json").write_bytes(PUBLIC)
    assert_private(client, path)


@pytest.mark.parametrize(
    "target",
    [
        ".aeri/runs/r/evidence.json",
        "meta/.aeri/runs/r/evidence.json",
        "private/evidence.json",
    ],
)
def test_asset_symlink_canonical_destination_must_remain_public(asset_route, target):
    client, root, _ = asset_route
    secret = root / target
    secret.parent.mkdir(parents=True)
    secret.write_bytes(PRIVATE_MARKER)
    (root / "meta/link.json").symlink_to(secret)
    assert_private(client, "meta/link.json")


def test_whole_asset_directory_link_cannot_alias_private_tree(asset_route):
    client, root, _ = asset_route
    secret = root / ".aeri/runs/r/evidence.json"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(PRIVATE_MARKER)
    (root / "meta").rmdir()
    (root / "meta").symlink_to(secret.parent, target_is_directory=True)
    assert_private(client, "meta/evidence.json")


def test_registered_dataset_root_inside_private_tree_is_refused(
    asset_route, monkeypatch
):
    client, root, service = asset_route
    private_root = root / ".aeri/runs/r/dataset"
    (private_root / "meta").mkdir(parents=True)
    (private_root / "meta/evidence.json").write_bytes(PRIVATE_MARKER)
    monkeypatch.setattr(service, "local_root", lambda repo: private_root)
    assert_private(client, "meta/evidence.json")


@pytest.mark.parametrize("linked", [False, True])
def test_public_assets_keep_get_head_range_and_internal_symlinks(asset_route, linked):
    client, root, _ = asset_route
    target = root / "data/info.json"
    target.write_bytes(PUBLIC)
    path = "data/info.json"
    if linked:
        (root / "meta/link.json").symlink_to(target)
        path = "meta/link.json"
    url = "/api/levi/files/test/" + path
    assert client.get(url).content == PUBLIC
    head = client.head(url)
    assert head.status_code == 200 and not head.content
    response = client.get(url, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == PUBLIC[:4]
    assert response.headers["cache-control"] == "no-cache"


def test_path_resolution_error_is_forbidden_but_catalog_errors_are_not_remapped(
    asset_route, monkeypatch
):
    from fastapi import HTTPException

    _, _, service = asset_route

    def broken(*args):
        raise OSError("unreadable path")

    monkeypatch.setattr(service, "inside", broken)
    with pytest.raises(HTTPException) as caught:
        service.dataset_file("test", "meta/file.json")
    assert caught.value.status_code == 403

    def catalog_error(*args):
        raise ValueError("catalog lookup failed")

    monkeypatch.setattr(service, "local_root", catalog_error)
    with pytest.raises(ValueError, match="catalog lookup failed"):
        service.dataset_file("test", "meta/file.json")
