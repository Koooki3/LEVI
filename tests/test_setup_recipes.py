"""Setup recipes (``levi/setup/recipes.py``): the schema, the safety
validator (class 3 and 4 are never executable, the policy server exception
needs a confirm record), and drift detection against a small fixture guide."""

import json
import sys

import pytest

from levi.setup import recipes as R

GUIDE = """# Operator guide

Intro text.

## 1. Order

```bash
cd ~/robot
scripts/preflight.sh
scripts/status.sh
nvidia-smi --query-gpu=memory.used --format=csv
ss -ltnp
```

## 2. Arm and gripper

### 2.1 Desk

Open the Desk page, unlock the joints, activate FCI.

```bash
~/robot/scripts/preflight.sh
```

### 2.2 Terminal A: arm stack

```bash
source ~/robot/env.sh
ros2 launch arm impedance.launch.py robot_ip:=10.0.0.2
```

Some text.

```bash
# ## not a heading inside code
ros2 control list_controllers
```

## 5. Policy server

```bash
uv run scripts/serve_policy.py --port 8000
```
"""


def src(guide, section, block=None, pick=None, text=False, **extra):
    g = R.parse_guide(guide)
    (sec,) = g.by_number(section)
    found = R.excerpt(sec, block, tuple(pick) if pick else None)
    out = {
        "section": section,
        "heading": sec.title,
        "sha256": found.sha256,
        "lines": [found.first, found.last],
    }
    if block is not None:
        out["block"] = block
    if pick:
        out["pick"] = list(pick)
    if text:
        out["text"] = found.text
    out.update(extra)
    return out


def recipe(rid, guide=GUIDE, section="2.2", block=1, pick=None, text=False, **fields):
    data = {
        "id": rid,
        "title": f"recipe {rid}",
        "risk": 1,
        "ui": "copy",
        "source": src(guide, section, block, pick, text),
    }
    data.update(fields)
    return data


def book(*items):
    return R.from_data({"version": 1, "recipe": list(items)})


def one(guide_now, item):
    b = book(item)
    assert b.problems == []
    (finding,) = R.check(b, guide_now)
    return finding


# ------------------------------------------------------------------ parsing


def test_sections_blocks_and_code_headings():
    g = R.parse_guide(GUIDE)
    assert g.problems == []
    numbers = [s.number for s in g.sections if s.number]
    assert numbers == ["1", "2", "2.1", "2.2", "5"]
    (arm,) = g.by_number("2.2")
    assert arm.title == "Terminal A: arm stack"
    assert len(arm.blocks) == 2  # "# ## not a heading" stays code
    assert arm.blocks[1].lines[0].startswith("# ## not a heading")


def test_line_numbers_and_pick():
    g = R.parse_guide(GUIDE)
    (order,) = g.by_number("1")
    found = R.excerpt(order, 1, (4, 5))
    assert found.text == "nvidia-smi --query-gpu=memory.used --format=csv\nss -ltnp"
    lines = GUIDE.splitlines()
    assert lines[found.first - 1].startswith("nvidia-smi")
    assert lines[found.last - 1] == "ss -ltnp"


def test_whitespace_and_line_endings_are_not_drift():
    item = recipe("R-A")
    crlf = GUIDE.replace("\n", "\r\n").replace("env.sh", "env.sh   ")
    assert one(crlf, item).status == "ok"


# ------------------------------------------------------------------ drift


def test_everything_in_step():
    b = book(
        recipe("R-CHK", section="1", pick=(4, 5)),
        recipe("R-DSK", section="2.1", block=None),
        recipe("R-A", risk=3, touches={"robot": True, "commands_robot": True}),
        recipe("R-A-chk", block=2, requires=["R-A"]),
    )
    assert b.problems == []
    assert [f.status for f in R.check(b, GUIDE)] == ["ok"] * 4


def test_one_changed_character_is_drift_with_a_difference():
    item = recipe("R-A", text=True)
    now = GUIDE.replace("robot_ip:=10.0.0.2", "robot_ip:=10.0.0.3")
    finding = one(now, item)
    assert finding.status == "drift"
    assert "changed" in finding.reasons[-1]
    assert "-ros2 launch arm impedance.launch.py robot_ip:=10.0.0.2" in finding.diff
    assert "+ros2 launch arm impedance.launch.py robot_ip:=10.0.0.3" in finding.diff


def test_one_changed_character_in_prose_is_drift():
    item = recipe("R-DSK", section="2.1", block=None)
    assert (
        one(GUIDE.replace("unlock the joints", "unlock the joint"), item).status
        == "drift"
    )


def test_pick_only_covers_its_lines():
    item = recipe("R-CHK", section="1", pick=(4, 5))
    assert (
        one(GUIDE.replace("scripts/status.sh", "scripts/state.sh"), item).status == "ok"
    )
    assert one(GUIDE.replace("ss -ltnp", "ss -ltn"), item).status == "drift"


def test_lines_added_above_only_shift():
    item = recipe("R-A")
    now = GUIDE.replace("Intro text.", "Intro text.\n\nMore.\nAnd more.")
    finding = one(now, item)
    assert finding.status == "ok" and finding.shifted
    assert finding.lines_now[0] == item["source"]["lines"][0] + 3


def test_reordered_blocks_are_reported_as_moved():
    item = recipe("R-A-chk", block=2)
    first = "```bash\nsource ~/robot/env.sh\nros2 launch arm impedance.launch.py robot_ip:=10.0.0.2\n```"
    second = (
        "```bash\n# ## not a heading inside code\nros2 control list_controllers\n```"
    )
    now = (
        GUIDE.replace(first, "@@FIRST@@")
        .replace(second, first)
        .replace("@@FIRST@@", second)
    )
    finding = one(now, item)
    assert finding.status == "moved"
    assert finding.found_at["section"] == "2.2" and finding.found_at["block"] == 1


def test_renumbered_section_is_found_by_its_text():
    item = recipe("R-D", section="5", touches={"listens": [8000], "gpu": True})
    finding = one(GUIDE.replace("## 5. Policy server", "## 6. Policy server"), item)
    assert finding.status == "moved"
    assert finding.found_at["section"] == "6"
    assert any("titled 'Policy server' is now 6" in r for r in finding.reasons)


def test_missing_section_and_missing_block():
    gone = GUIDE.split("## 5. Policy server")[0]
    assert one(gone, recipe("R-D", section="5")).status == "missing"
    item = recipe("R-A-chk", block=2)
    now = GUIDE.replace(
        "```bash\n# ## not a heading inside code\nros2 control list_controllers\n```",
        "",
    )
    finding = one(now, item)
    assert finding.status == "missing"
    assert "has 1 code block(s), the recipe names block 2" in finding.reasons[0]


def test_pick_beyond_the_block_is_missing():
    item = recipe("R-CHK", section="1", pick=(4, 5))
    now = GUIDE.replace("ss -ltnp\n", "")
    finding = one(now, item)
    assert finding.status in ("missing", "moved")
    assert finding.status == "missing"


def test_duplicate_section_number_is_ambiguous():
    now = GUIDE + "\n## 2.2 Another one\n\n```bash\necho\n```\n"
    finding = one(now, recipe("R-A"))
    assert finding.status == "ambiguous"
    assert "more than once" in finding.reasons[0]


def test_unclosed_fence_is_a_guide_error():
    now = GUIDE + "\n## 9. Broken\n\n```bash\necho never closed\n"
    finding = one(now, recipe("R-A"))
    assert finding.status == "doc_error"
    assert "never closed" in finding.reasons[0]


def test_heading_change_alone_is_drift():
    finding = one(
        GUIDE.replace("Terminal A: arm stack", "Terminal A: arm"), recipe("R-A")
    )
    assert finding.status == "drift"
    assert "heading changed" in finding.reasons[0]


# ------------------------------------------------------------------ safety


@pytest.mark.parametrize(
    "fields, problem",
    [
        ({"risk": 3, "ui": "execute"}, "risk 3 cannot be execute"),
        ({"risk": 3, "ui": "native"}, "cannot be native"),
        ({"risk": 4, "ui": "execute", "touches": {"moves": True}}, "risk 4"),
        ({"risk": 4, "ui": "native", "touches": {"moves": True}}, "risk 4"),
        ({"risk": 2, "touches": {"moves": True}}, "risk must be 4"),
        ({"risk": 2, "touches": {"commands_robot": True}}, "risk must be 3 or 4"),
        (
            {"risk": 1, "ui": "native", "touches": {"connects": [5000]}},
            "robot-side port",
        ),
        (
            {"risk": 1, "ui": "execute", "touches": {"connects": [8000]}},
            "robot-side port",
        ),
        (
            {"risk": 3, "ui": "execute", "kind": "policy_server"},
            "needs confirm",
        ),
        (
            {
                "risk": 3,
                "ui": "execute",
                "kind": "policy_server",
                "confirm": {"required": True, "decision": "user, 2026-10-10"},
                "touches": {"commands_robot": True},
            },
            "only load a model",
        ),
        (
            {
                "risk": 3,
                "ui": "execute",
                "kind": "policy_server",
                "confirm": {"required": False, "decision": "x"},
            },
            "needs confirm",
        ),
    ],
)
def test_unsafe_recipes_are_refused(fields, problem):
    b = book(recipe("R-X", **fields))
    assert b.recipes == []
    assert any(problem in text for _, text in b.problems), b.problems


def test_policy_server_may_be_executable_with_a_confirm_record():
    item = recipe(
        "R-D1",
        section="5",
        risk=3,
        ui="execute",
        kind="policy_server",
        confirm={"required": True, "decision": "user allowed it, 2026-10-10"},
        touches={"gpu": True, "listens": [8000]},
    )
    b = book(item)
    assert b.problems == []
    assert b.recipes[0].executable
    assert b.recipes[0].public()["touches"]["listens"] == [8000]


def test_no_class_3_or_4_recipe_is_ever_executable():
    """Exhaustive over every risk x ui x kind: what survives validation as
    executable is a confirmed, model-only policy server or class 1-2."""
    for risk in R.RISKS:
        for ui in R.UI_MODES:
            for kind in ("", "policy_server", "robot_server"):
                b = book(
                    recipe(
                        "R-X",
                        risk=risk,
                        ui=ui,
                        kind=kind,
                        touches={"moves": risk == 4},
                        confirm={"required": True, "decision": "d"},
                    )
                )
                for r in b.recipes:
                    if r.executable and r.risk >= 3:
                        assert r.risk == 3 and r.kind == "policy_server"
                        assert not (r.moves or r.commands_robot or r.robot)


# ------------------------------------------------------------------ schema


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"id": "bad id"}, "id must be"),
        ({"risk": 5}, "risk must be"),
        ({"risk": True}, "risk must be"),
        ({"ui": "run"}, "ui must be"),
        ({"colour": "red"}, "unknown field"),
        ({"title": ""}, "title is required"),
        ({"source": {"section": "2.2", "block": 1, "sha256": "abc"}}, "sha256"),
        (
            {"source": {"section": "two", "block": 1, "sha256": "0" * 64}},
            "section number",
        ),
        ({"source": {"section": "2.2", "block": 0, "sha256": "0" * 64}}, "block"),
        ({"source": {"section": "2.2", "sha256": "0" * 64, "pick": [3, 2]}}, "pick"),
        (
            {"source": {"section": "2.2", "sha256": "0" * 64, "text": "x"}},
            "source.text",
        ),
        (
            {"source": {"section": "2.2", "sha256": "0" * 64, "where": 1}},
            "unknown source",
        ),
        ({"touches": {"listens": [70000]}}, "port numbers"),
        ({"touches": {"robot": "yes"}}, "true or false"),
        ({"requires": "R-A"}, "list of strings"),
    ],
)
def test_bad_fields_are_named(change, problem):
    item = recipe("R-A")
    item.update(change)
    b = book(item)
    assert b.recipes == []
    assert any(problem in text for _, text in b.problems), b.problems


def test_duplicate_unknown_and_circular_requires():
    b = book(
        recipe("R-A", requires=["R-B"]),
        recipe("R-B", requires=["R-A"]),
        recipe("R-A"),
        recipe("R-C", requires=["R-NONE"]),
    )
    texts = [f"{i}: {t}" for i, t in b.problems]
    assert any("R-A: duplicate id" in t for t in texts)
    assert any("R-NONE" in t for t in texts)
    assert any("cycle" in t for t in texts)


def test_version_and_shape_errors():
    with pytest.raises(R.RecipeError):
        R.from_data({"version": 2, "recipe": []})
    with pytest.raises(R.RecipeError):
        R.from_data({"recipe": {"id": "x"}})
    with pytest.raises(R.RecipeError):
        R.from_data([1, 2])


def toml_of(item):
    s = item["source"]
    lines = [
        "version = 1",
        "",
        "[[recipe]]",
        f'id = "{item["id"]}"',
        f'title = "{item["title"]}"',
        f"risk = {item['risk']}",
        f'ui = "{item["ui"]}"',
        "[recipe.source]",
        f'section = "{s["section"]}"',
        f'heading = "{s["heading"]}"',
        f"block = {s['block']}",
        f"lines = {s['lines']}",
        f'sha256 = "{s["sha256"]}"',
        "[recipe.touches]",
        "robot = true",
        "commands_robot = true",
    ]
    return "\n".join(lines) + "\n"


def test_load_toml_and_json(tmp_path):
    item = recipe("R-A", risk=3)
    toml_file = tmp_path / "recipes.toml"
    toml_file.write_text(toml_of(item))
    b = R.load(toml_file)
    assert b.problems == [] and b.recipes[0].robot and b.recipes[0].commands_robot
    json_file = tmp_path / "recipes.json"
    json_file.write_text(json.dumps({"version": 1, "recipe": [item]}))
    assert R.load(json_file).recipes[0].id == "R-A"


def test_unreadable_files(tmp_path):
    with pytest.raises(R.RecipeError):
        R.load(tmp_path / "absent.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[[recipe]\n")
    with pytest.raises(R.RecipeError):
        R.load(bad)
    big = tmp_path / "big.toml"
    big.write_bytes(b"#" * (R.MAX_FILE_BYTES + 1))
    with pytest.raises(R.RecipeError):
        R.load(big)


# ------------------------------------------------------------------ command line


@pytest.fixture
def files(tmp_path):
    guide = tmp_path / "setup.md"
    guide.write_text(GUIDE)
    rec = tmp_path / "recipes.toml"
    rec.write_text(toml_of(recipe("R-A", risk=3)))
    return guide, rec


def test_cli_check_exit_codes(files, capsys, monkeypatch):
    guide, rec = files
    assert R.main(["check", "--recipes", str(rec), "--doc", str(guide)]) == 0
    assert "1 of 1 recipe(s) in step" in capsys.readouterr().out
    guide.write_text(GUIDE.replace("10.0.0.2", "10.0.0.9"))
    assert R.main(["check", "--recipes", str(rec), "--doc", str(guide), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["findings"][0]["status"] == "drift"
    assert (
        R.main(["check", "--recipes", str(rec), "--doc", str(guide.parent / "no.md")])
        == 2
    )
    # The settings name the files when no option does.
    monkeypatch.setenv(R.ENV_RECIPES, str(rec))
    monkeypatch.setenv(R.ENV_DOC, str(guide))
    assert R.main(["check"]) == 1


def test_cli_refused_recipe_fails_the_check(files, capsys):
    guide, rec = files
    rec.write_text(
        toml_of(recipe("R-A", risk=3)).replace('ui = "copy"', 'ui = "execute"')
    )
    assert R.main(["check", "--recipes", str(rec), "--doc", str(guide)]) == 1
    assert "refused R-A: risk 3 cannot be execute" in capsys.readouterr().out


def test_cli_excerpt(files, capsys):
    guide, _ = files
    assert (
        R.main(
            [
                "excerpt",
                "--doc",
                str(guide),
                "--section",
                "1",
                "--block",
                "1",
                "--pick",
                "4",
                "5",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["sha256"] == R.digest(
        "nvidia-smi --query-gpu=memory.used --format=csv\nss -ltnp"
    )
    assert out["lines"][1] - out["lines"][0] == 1
    assert R.main(["excerpt", "--doc", str(guide), "--section", "7"]) == 1


def test_levi_setup_recipes_dispatch_does_not_configure(files, monkeypatch, capsys):
    from levi import cli

    guide, rec = files

    def no(*_):
        raise AssertionError("configure() must not run for a read-only check")

    monkeypatch.setattr(cli, "configure", no)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "levi",
            "setup",
            "recipes",
            "check",
            "--recipes",
            str(rec),
            "--doc",
            str(guide),
        ],
    )
    assert cli.main() == 0
    assert "in step" in capsys.readouterr().out


def test_check_never_writes(files):
    guide, rec = files
    before = {p: p.stat().st_mtime_ns for p in (guide, rec)}
    R.main(["check", "--recipes", str(rec), "--doc", str(guide)])
    assert {p: p.stat().st_mtime_ns for p in (guide, rec)} == before
    assert sorted(p.name for p in guide.parent.iterdir()) == [
        "recipes.toml",
        "setup.md",
    ]
