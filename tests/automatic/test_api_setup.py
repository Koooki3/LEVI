# ruff: noqa: F401, F811
"""``GET /api/levi/automatic/setup-guide`` (T-API-1): steps from the
operator guide's recipes and the read-only probes; nothing is executed."""

import json
import subprocess

import pytest
from test_api_common import client, home, roots

from levi.automatic import setup_guide
from levi.setup import recipes as R

BASE = "/api/levi/automatic"

GUIDE = """# Operator guide

## 1. Order

```bash
cd ~/robot
scripts/preflight.sh
```

## 2. Arm stack

```bash
source ~/robot/env.sh
ros2 launch arm impedance.launch.py robot_ip:=10.0.0.2
```

## 3. Live service

```bash
systemctl --user start levi-live
```
"""


def source(guide, section, block=1):
    sec = R.parse_guide(guide).by_number(section)[0]
    found = R.excerpt(sec, block, None)
    return {
        "section": section,
        "heading": sec.title,
        "block": block,
        "sha256": found.sha256,
    }


def recipe(rid, section, **fields):
    return {
        "id": rid,
        "title": f"recipe {rid}",
        "risk": 1,
        "ui": "copy",
        "source": source(GUIDE, section),
        **fields,
    }


@pytest.fixture
def guide_files(tmp_path, monkeypatch):
    doc = tmp_path / "setup.md"
    doc.write_text(GUIDE)
    recipes = tmp_path / "recipes.json"
    recipes.write_text(
        json.dumps(
            {
                "version": 1,
                "recipe": [
                    recipe("R-ORDER", "1", ui="native"),
                    recipe(
                        "R-LIVE",
                        "3",
                        risk=2,
                        ui="execute",
                        requires=["R-ORDER"],
                        touches={"listens": [7881]},
                    ),
                    recipe("R-ARM", "2", risk=3, requires=["R-ORDER"]),
                ],
            }
        )
    )
    monkeypatch.setenv(setup_guide.RECIPES_ENV, str(recipes))
    monkeypatch.setenv(setup_guide.DOC_ENV, str(doc))
    return doc, recipes


def probe(listening):
    return {
        "ports": {
            "state": "ok",
            "ports": [{"port": p, "listening": True} for p in listening],
        }
    }


def by_id(found):
    return {s["id"]: s for s in found}


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the guide runs nothing")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("os.system", refuse)


def test_without_recipes_the_manual_and_native_steps_remain(monkeypatch):
    monkeypatch.delenv(setup_guide.RECIPES_ENV, raising=False)
    monkeypatch.delenv(setup_guide.DOC_ENV, raising=False)
    found = by_id(setup_guide.steps(probe([7860, 7861])))
    assert list(found) == [
        "estop",
        "arm-enabled",
        "product",
        "live-service",
        "vllm",
        "recipes-not-configured",
        "scene-objects",
    ]
    for step in found.values():
        assert set(step) >= {"id", "title", "level", "mode", "why", "status"}
        assert set(step["title"]) == set(step["why"]) == {"en", "zh"}
        assert step["level"] in (1, 2, 3, 4)
        assert step["mode"] in ("native", "execute", "copy")
        assert "command" not in step or step["mode"] != "native"
    assert found["estop"]["mode"] == "copy" and found["estop"]["status"] == "todo"
    assert found["product"]["status"] == "ok"
    assert found["live-service"]["status"] == "todo"
    assert found["recipes-not-configured"]["status"] == "unknown"


def test_a_failing_probe_makes_native_steps_unknown(monkeypatch):
    monkeypatch.delenv(setup_guide.RECIPES_ENV, raising=False)
    found = by_id(setup_guide.steps({"ports": {"state": "unknown", "detail": "x"}}))
    assert {found[k]["status"] for k in ("product", "live-service", "vllm")} == {
        "unknown"
    }
    assert "detail" in found["product"]
    assert setup_guide.steps({}) == setup_guide.steps({"ports": None})


def test_recipes_give_commands_status_and_never_execute(guide_files):
    found = by_id(setup_guide.steps(probe([7881])))
    assert found["R-LIVE"]["command"] == "systemctl --user start levi-live"
    # An execute recipe is shown as copy: this version runs nothing.
    assert found["R-LIVE"]["mode"] == "copy" and found["R-LIVE"]["level"] == 2
    assert found["R-LIVE"]["status"] == "ok"
    assert found["R-LIVE"]["requires"] == ["R-ORDER"]
    assert found["R-ARM"]["level"] == 3 and found["R-ARM"]["status"] == "todo"
    assert "ros2 launch arm impedance" in found["R-ARM"]["command"]
    # A native recipe has no command to copy.
    assert found["R-ORDER"]["mode"] == "native" and "command" not in found["R-ORDER"]
    assert "recipes-not-configured" not in found
    assert by_id(setup_guide.steps(probe([])))["R-LIVE"]["status"] == "todo"


def test_a_changed_guide_hides_the_command_and_warns(guide_files):
    doc, _ = guide_files
    doc.write_text(GUIDE.replace("robot_ip:=10.0.0.2", "robot_ip:=10.0.0.9"))
    found = by_id(setup_guide.steps(probe([])))
    arm = found["R-ARM"]
    assert arm["status"] == "warn" and "command" not in arm
    assert "10.0.0.9" not in json.dumps(arm)
    assert found["R-LIVE"]["status"] == "todo" and "command" in found["R-LIVE"]


def test_unreadable_files_never_raise(guide_files, tmp_path):
    doc, recipes = guide_files
    doc.unlink()
    found = by_id(setup_guide.steps(probe([])))
    assert found["R-ARM"]["status"] == "unknown" and "command" not in found["R-ARM"]
    recipes.write_text("{not json")
    found = by_id(setup_guide.steps(probe([])))
    assert found["recipes-unreadable"]["status"] == "warn"
    assert str(tmp_path) not in json.dumps(found)


def test_the_route_returns_steps_and_hides_paths(
    client, guide_files, tmp_path, monkeypatch
):
    monkeypatch.setattr(setup_guide, "probe_status", lambda: probe([7860, 7861, 7881]))
    response = client.get(f"{BASE}/setup-guide")
    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["steps"]
    assert {s["id"] for s in body["steps"]} >= {"estop", "R-ARM", "scene-objects"}
    assert str(tmp_path) not in response.text
    # The probe failing outright still gives an answer.
    monkeypatch.setattr(setup_guide, "probe_status", dict)
    assert client.get(f"{BASE}/setup-guide").status_code == 200
