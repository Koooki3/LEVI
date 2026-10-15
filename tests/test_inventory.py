"""scripts/inventory.py: a loss shows, a move or an addition does not."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inventory.py"


def load(root: Path):
    spec = importlib.util.spec_from_file_location("inventory_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    return module


def test_routes_are_keyed_by_what_a_caller_sees_not_by_file(tmp_path):
    (tmp_path / "levi").mkdir()
    (tmp_path / "levi/a.py").write_text(
        "@app.get('/x')\ndef x(): ...\n"
        "@app.api_route('/y', methods=['GET', 'HEAD'])\ndef y(): ...\n"
        "@router.post('/z')\nasync def z(): ...\n"
    )
    inv = load(tmp_path)
    found = inv.routes(["levi/a.py"])
    assert set(found) == {"GET /x", "GET /y", "HEAD /y", "POST /z"}
    (tmp_path / "levi/b.py").write_text((tmp_path / "levi/a.py").read_text())
    (tmp_path / "levi/a.py").unlink()
    assert set(inv.routes(["levi/b.py"])) == set(found)  # moved, not lost


def test_diff_reports_a_lost_item_and_a_changed_hashed_file_but_not_additions(tmp_path):
    inv = load(tmp_path)
    old = {
        "routes": {"GET /a": "f:a", "GET /b": "f:b"},
        "hashed_files": {"levi/knowledge/x.md": "aaaa", "levi/knowledge/y.md": "bbbb"},
        "frontend": {"i18n": {"en": ["k1", "k2"]}},
    }
    new = {
        "routes": {"GET /a": "g:a", "GET /c": "g:c"},
        "hashed_files": {"levi/knowledge/x.md": "cccc"},
        "frontend": {"i18n": {"en": ["k1", "k2", "k3"]}},
    }
    lost, notes = inv.diff(old, new)
    assert "lost routes: GET /b" in lost
    assert "changed file: levi/knowledge/x.md" in lost
    assert "lost file: levi/knowledge/y.md" in lost
    assert not any("GET /c" in line or "GET /a" in line for line in lost)
    assert any("routes: +1 new" in n for n in notes)


def test_identical_inventories_lose_nothing(tmp_path):
    inv = load(tmp_path)
    same = {"routes": {"GET /a": "f"}, "frontend": {"pages": ["/"], "i18n": {}}}
    assert inv.diff(same, same)[0] == []


def test_a_router_prefix_is_part_of_the_route_and_colliding_keys_name_their_file(
    tmp_path,
):
    (tmp_path / "levi").mkdir()
    (tmp_path / "levi/pool.py").write_text(
        "router = APIRouter(prefix='/api/levi/pool')\n@router.get('/status')\ndef s(): ...\n"
    )
    (tmp_path / "levi/live.py").write_text("@app.get('/status')\ndef s(): ...\n")
    (tmp_path / "levi/other.py").write_text("@app.get('/status')\ndef s(): ...\n")
    inv = load(tmp_path)
    keys = set(inv.routes(["levi/pool.py", "levi/live.py", "levi/other.py"]))
    assert "GET /api/levi/pool/status" in keys
    assert {"GET /status @levi/live.py", "GET /status @levi/other.py"} <= keys


def test_mounts_record_the_prefix_a_router_is_included_under(tmp_path):
    (tmp_path / "levi").mkdir()
    (tmp_path / "levi/a.py").write_text(
        "app.include_router(pool_router, prefix='/api/levi/pool')\napp.mount('/annotations', x)\n"
    )
    inv = load(tmp_path)
    assert set(inv.mounts(["levi/a.py"])) == {
        "include_router pool_router prefix=/api/levi/pool",
        "mount /annotations",
    }


def test_a_missing_section_a_dropped_heading_and_a_dev_dependency_are_losses(tmp_path):
    inv = load(tmp_path)
    old = {
        "frontend": {"i18n": {"en": ["k"], "zh": ["k"]}},
        "docs": {"docs/A.md": 12},
        "dependencies": {
            "node_dev": {"prettier": "3"},
            "python_groups": {"dev": ["pytest"]},
        },
    }
    new = {
        "frontend": {"i18n": {"en": ["k"]}},
        "docs": {"docs/A.md": 1},
        "dependencies": {"node_dev": {}, "python_groups": {"dev": []}},
    }
    lost, _ = inv.diff(old, new)
    text = "\n".join(lost)
    assert "i18n.zh: k" in text
    assert "docs/A.md heading#2" in text
    assert "node_dev_deps: prettier" in text and "python_groups: dev: pytest" in text


def test_deleting_one_of_two_same_named_commands_or_titles_is_a_loss(tmp_path):
    inv = load(tmp_path)
    old = {
        "cli": {"list": ["a.py", "b.py"]},
        "frontend": {"test_titles": ["f::t", "f::t"]},
    }
    new = {"cli": {"list": ["a.py"]}, "frontend": {"test_titles": ["f::t"]}}
    lost, _ = inv.diff(old, new)
    assert "lost cli: list#2" in lost and "lost frontend.test_titles: f::t#2" in lost
    moved = {
        "cli": {"list": ["c.py", "d.py"]},
        "frontend": {"test_titles": ["f::t", "f::t"]},
    }
    assert inv.diff(old, moved)[0] == []


def test_an_error_recorded_on_either_side_makes_the_comparison_fail(tmp_path):
    inv = load(tmp_path)
    ok = {"capabilities": {"a": "read"}}
    broken = {"capabilities": {"__error__": "ModuleNotFoundError"}}
    assert any("cannot compare capabilities" in x for x in inv.diff(ok, broken)[0])
    assert any("cannot compare capabilities" in x for x in inv.diff(broken, ok)[0])
