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
