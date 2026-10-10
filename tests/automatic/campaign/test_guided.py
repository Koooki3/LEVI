"""Guided legacy-client campaign, data side (T-CP-11,
levi/automatic/campaign/guided.py): commands rendered from the operator
guide with only the allowed parameters replaced (and refused when the guide
drifted), rollouts collected per segment, cards that need a person,
agreement apart by how the episode ended. Synthetic guide and rollouts; the
maintainer's own guide is read (only read) when it is there."""

import json
import re
from pathlib import Path

import pytest
import test_ledger_fixtures as fx

from levi.automatic.campaign import guided as G
from levi.automatic.campaign import ledger as L
from levi.automatic.campaign import report as R


def write_report(*args, **kwargs):
    return R.write_report(*args, campaign_state="REPORTED", **kwargs)


# A guide shaped like setup.md §6.2-6.4, with made-up machine values.
GUIDE = """# Operator guide

## 6. Terminal E

### 6.2 Trial run

```bash
uv run client.py --levi-mode dual --eval-num 2 \\
  --rollout-group demo_ckpt --eval-note "trial"
```

### 6.3 Dual-label evaluation

Text before the command.

```bash
conda deactivate
cd ~/work/policy
uv run --with pyrealsense2==2.55.1.6486 examples/client.py \\
  --remote-host=127.0.0.1 --remote-port=8000 \\
  --view-camera-id 111111111111 --wrist-camera-id 222222222222 \\
  --prompt "pick the eggplant in the blue plate" \\
  --eval-num 50 --min-z 0.05 \\
  --home-pose 0.48 -0.01 0.23 3.11 0.00 -0.88 \\
  --levi-mode dual --new --no-display-images \\
  --rollout-group pi05_fr3_all_step49999 \\
  --eval-note "pi05_fr3_all_step49999 正式双标签评测，步数由终端输入"
```

### 6.4 Unattended

```bash
uv run client.py --levi-mode on --eval-num 50 \\
  --rollout-group x --eval-note "y"
```
"""


def base():
    return G.base_command(GUIDE)


def changed_lines(a: str, b: str) -> list:
    return [
        (x, y) for x, y in zip(a.splitlines(), b.splitlines(), strict=True) if x != y
    ]


# ----------------------------------------------------------------- commands


def test_the_command_comes_from_section_6_3_block_1():
    b = base()
    assert b.section == "6.3" and b.block == 1
    assert b.text.startswith("conda deactivate\n") and "--levi-mode dual" in b.text
    assert G.BaseCommand.from_dict(b.to_dict()) == b
    with pytest.raises(G.GuidedError, match="digest"):
        G.BaseCommand.from_dict({**b.to_dict(), "sha256": "0" * 64})


def test_render_replaces_only_the_allowed_parameters():
    b = base()
    note = G.eval_note("c20261010-eggplant", 3, "X2")
    out = G.render(
        b,
        eval_num=5,
        rollout_group="recap_cfg_r2_best_step14300_jax",
        eval_note=note,
    )
    assert changed_lines(b.text, out) == [
        ("  --eval-num 50 --min-z 0.05 \\", "  --eval-num 5 --min-z 0.05 \\"),
        (
            "  --rollout-group pi05_fr3_all_step49999 \\",
            "  --rollout-group recap_cfg_r2_best_step14300_jax \\",
        ),
        (
            '  --eval-note "pi05_fr3_all_step49999 正式双标签评测，步数由终端输入"',
            '  --eval-note "c20261010-eggplant s03 X2"',
        ),
    ]
    assert "--levi-mode dual" in out and "--new" in out
    # The task instruction is no per-segment parameter: it is the
    # campaign's shared setting, frozen into the base command once.
    with pytest.raises(TypeError):
        G.render(b, eval_num=5, rollout_group="g", eval_note=note, prompt="x")
    frozen = G.base_command(GUIDE, prompt="stack the cups")
    assert G.BaseCommand.from_dict(frozen.to_dict()) == frozen
    with_prompt = G.render(frozen, eval_num=5, rollout_group="g", eval_note=note)
    assert '--prompt "stack the cups"' in with_prompt
    assert len(changed_lines(b.text, with_prompt)) == 4
    lay = fx.layout(per_arm=10, segment_trials=5)
    cmds = G.segment_commands(frozen, lay, {"A": "a1", "B": "b1"})
    assert all('--prompt "stack the cups"' in c["command"] for c in cmds)
    with pytest.raises(TypeError):
        G.segment_commands(frozen, lay, {"A": "a1", "B": "b1"}, prompt="other")
    with pytest.raises(G.GuidedError, match="no single --prompt"):
        G.base_command(GUIDE, section="6.2", prompt="x")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("eval_note", 'x" --levi-mode on "'),
        ("eval_note", "x $(rm -rf ~)"),
        ("eval_note", "x `id`"),
        ("eval_note", "two\nlines"),
        ("eval_note", "bang!"),
        ("eval_note", "back\\slash"),
        ("rollout_group", "../../etc"),
        ("rollout_group", "two words"),
        ("rollout_group", "a;b"),
    ],
)
def test_values_that_would_change_the_command_are_refused(field, value):
    values = {"eval_num": 5, "rollout_group": "g", "eval_note": "c1 s01 X1"}
    values[field] = value
    with pytest.raises(G.GuidedError):
        G.render(base(), **values)


def test_an_unsafe_shared_prompt_is_refused():
    for bad in ('pick "it"', "pick $(id)", "two\nlines"):
        with pytest.raises(G.GuidedError):
            G.base_command(GUIDE, prompt=bad)
    saved = G.base_command(GUIDE, prompt="ok").to_dict()
    with pytest.raises(G.GuidedError):
        G.BaseCommand.from_dict({**saved, "prompt": "x `id`"})


def test_the_trial_count_must_be_a_positive_whole_number():
    for bad in (0, -1, True, 2.5, "5"):
        with pytest.raises(G.GuidedError, match="eval_num"):
            G.render(base(), eval_num=bad, rollout_group="g", eval_note="c1 s01")


def test_a_section_that_is_not_one_dual_label_command_is_refused():
    with pytest.raises(G.GuidedError, match="not a dual-label"):
        G.base_command(GUIDE, section="6.4")
    with pytest.raises(G.GuidedError, match="appears 0 times"):
        G.base_command(GUIDE, section="9.9")
    with pytest.raises(G.GuidedError, match="no code block 2"):
        G.base_command(GUIDE, block=2)
    twice = GUIDE.replace("### 6.4 Unattended", "### 6.3 Again")
    with pytest.raises(G.GuidedError, match="appears 2 times"):
        G.base_command(twice)
    no_note = GUIDE.replace(
        '  --eval-note "pi05_fr3_all_step49999 正式双标签评测，步数由终端输入"', "  --x"
    )
    with pytest.raises(G.GuidedError, match="--eval-note once"):
        G.base_command(no_note)
    with pytest.raises(G.GuidedError, match="never closed"):
        G.base_command(GUIDE + "\n```bash\nopen fence\n")


def test_a_changed_guide_is_drift_with_a_diff():
    snapshot = base()
    assert G.check_drift(snapshot, GUIDE) is None
    moved = GUIDE.replace("--min-z 0.05", "--min-z 0.02")
    diff = G.check_drift(snapshot, moved)
    assert "-  --eval-num 50 --min-z 0.05 \\" in diff
    assert "+  --eval-num 50 --min-z 0.02 \\" in diff
    with pytest.raises(G.GuideDrift) as caught:
        G.render_checked(
            snapshot, moved, eval_num=5, rollout_group="g", eval_note="c1 s01 X1"
        )
    assert caught.value.diff == diff
    # The section gone is drift as well.
    gone = GUIDE.replace("### 6.3 Dual-label evaluation", "### 6.9 Elsewhere")
    assert "appears 0 times" in G.check_drift(snapshot, gone)
    # Unchanged guide: rendered as usual.
    assert G.render_checked(
        snapshot, GUIDE, eval_num=5, rollout_group="g", eval_note="c1 s01 X1"
    )


def find_setup_md():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "setup.md"
        if candidate.is_file() and "### 6.3" in candidate.read_text(errors="replace"):
            return candidate
    return None


def test_the_maintainers_guide_renders_word_for_word():
    """Run where the maintainer's setup.md sits above the checkout: the
    rendered command must be the guide's §6.3 command with only the allowed
    values changed. When the guide no longer fits (drift), this fails and
    prints the difference."""
    path = find_setup_md()
    if path is None:
        pytest.skip("no operator guide above this checkout")
    text = path.read_text()
    try:
        b = G.base_command(text)
    except G.GuidedError as exc:
        pytest.fail(f"setup.md §6.3 no longer gives a dual-label command: {exc}")
    out = G.render(
        b,
        eval_num=5,
        rollout_group="recap_cfg_r2_best_step14300_jax",
        eval_note="c1 s01 X1",
    )
    diff = changed_lines(b.text, out)
    assert len(diff) == 3, diff
    for before, after in diff:
        assert re.sub(
            r"--(eval-num|rollout-group|eval-note)[ =]\S.*", "", before
        ) == re.sub(r"--(eval-num|rollout-group|eval-note)[ =]\S.*", "", after)
    assert G.check_drift(b, text) is None


def test_eval_notes_round_trip():
    note = G.eval_note("c20261010-eggplant", 7, "X1")
    assert note == "c20261010-eggplant s07 X1"
    assert G.parse_note(note) == ("c20261010-eggplant", 7, "X1")
    assert G.parse_note("c1 s12") == ("c1", 12, None)
    for other in ("pi05 正式双标签评测", "c1 s1 X1", "", None, "c1 s01 X1 extra"):
        assert G.parse_note(other) is None
    with pytest.raises(L.LedgerError):
        G.eval_note("c1", 1, "X 1")


def test_one_command_per_segment_with_arm_codes():
    lay = fx.layout(per_arm=10, segment_trials=5)
    groups = {"A": "pi05_fr3_all_step49999", "B": "recap_cfg_r2_best_step14300_jax"}
    cmds = G.segment_commands(base(), lay, groups)
    assert [c["segment"] for c in cmds] == [1, 2, 3, 4]
    assert [c["code"] for c in cmds] == ["X1", "X2", "X2", "X1"]
    assert cmds[1]["eval_note"] == f"{fx.CAMPAIGN} s02 X2"
    assert "--eval-num 5 " in cmds[0]["command"]
    assert "--rollout-group recap_cfg_r2_best_step14300_jax" in cmds[1]["command"]
    # The note carries the code, never the arm's name.
    assert all(" A" not in c["eval_note"] and " B" not in c["eval_note"] for c in cmds)
    with pytest.raises(G.GuidedError, match="own code"):
        G.segment_commands(base(), lay, groups, codes={"A": "X1", "B": "X1"})
    with pytest.raises(G.GuidedError, match="no rollout group"):
        G.segment_commands(base(), lay, {"A": "g"})


# ---------------------------------------------------------------- rollouts


def write_rollout(
    root,
    group,
    task,
    number,
    *,
    note,
    run_id,
    operator="success",
    agent="success",
    ended_by="budget",
    complete=True,
    folder="demo",
    label_mode="dual",
    created="2026-10-10T10:00:00+0800",
    undecided=False,
):
    path = Path(root) / group / task / f"{folder}_{number:04d}"
    path.mkdir(parents=True)
    ev = {
        "episode_in_session": number,
        "eval_note": note,
        "max_steps": 400,
        "run_id": run_id,
        "outcome": operator if operator in ("success", "failure") else "discarded",
        "verdict_by": "operator",
        "counted": operator in ("success", "failure"),
        "steps": 400 if ended_by == "budget" else 120,
        "ended_by": ended_by,
        "operator_outcome": operator,
        "pid": 1234,
        "host": "robot-pc",
    }
    if label_mode:
        ev["label_mode"] = label_mode
    if agent is not None:
        ev["agent_label"] = {
            "source": "online",
            "status": "ok",
            "outcome": agent,
            "undecided": undecided,
            "spec": {"id": "generic-final", "version": 1},
            "timing": "after_budget",
        }
    meta = {
        "created_at": created,
        "stopped_at": created,
        "eval": ev,
        "cameras": {"wrist": {"serial": "222222222222"}},
    }
    (path / "metadata.json").write_text(json.dumps(meta))
    if complete:
        (path / ".complete").write_text("")
    return path


def test_rollouts_of_one_segment_are_collected_and_wait_for_cards(tmp_path):
    lay = fx.layout(per_arm=5, segment_trials=5)
    note = G.eval_note(fx.CAMPAIGN, 1, "X1")
    root, group, task = tmp_path, "pi05_fr3_all_step49999", "pick_eggplant"
    write_rollout(root, group, task, 1, note=note, run_id="run1")
    write_rollout(
        root,
        group,
        task,
        2,
        note=note,
        run_id="run1",
        operator="discarded",
        folder="discarded",
    )
    write_rollout(
        root,
        group,
        task,
        3,
        note=note,
        run_id="run1",
        operator="failure",
        agent="failure",
        ended_by="operator_key",
    )
    write_rollout(root, group, task, 4, note=note, run_id="run1", complete=False)
    # A rerun after an interruption: a new client run, later.
    write_rollout(
        root,
        group,
        task,
        5,
        note=note,
        run_id="run2",
        created="2026-10-10T11:00:00+0800",
    )
    # Other campaigns, other segments, not dual: ignored or left out.
    write_rollout(
        root, group, task, 6, note=G.eval_note(fx.CAMPAIGN, 2, "X2"), run_id="run3"
    )
    write_rollout(root, group, task, 7, note="pi05 正式双标签评测", run_id="run4")
    write_rollout(root, group, task, 8, note=note, run_id="run5", label_mode=None)
    write_rollout(root, group, task, 9, note=f"{fx.CAMPAIGN} s1 X1", run_id="run6")
    records = G.scan_rollouts(root, group, task)
    assert len(records) == 9 and records[0].key == f"{group}/{task}/demo_0001"
    got = G.collect_segment(records, lay, 1)
    assert got["runs"] == ["run1", "run2"]
    assert got["ignored"] == [
        {"key": f"{group}/{task}/demo_0008", "reason": "not a dual-label run"},
        {"key": f"{group}/{task}/demo_0009", "reason": "unreadable note"},
    ]
    statuses = [(r.number, r.status) for r in got["rows"]]
    assert statuses == [
        (1, "valid"),
        (2, "discarded"),
        (3, "valid"),
        (4, "incomplete"),
        (5, "valid"),
    ]
    assert [
        (p["key"].rsplit("/", 1)[1], p["candidate_card"]) for p in got["pending"]
    ] == [
        ("demo_0001", "r01c1"),
        ("demo_0003", "r01c1"),
        ("demo_0005", "r01c1"),
    ]
    assert got["counts"]["A"]["pairable"] == 0 and got["counts"]["A"]["missing"] == 5
    # A person confirms the cards: the episodes hold them and may pair.
    store = G.CardConfirmations(tmp_path / "campaign" / "cards.jsonl")
    store.add(f"{group}/{task}/demo_0001", "r01c1", by="op-1")
    store.add(f"{group}/{task}/demo_0003", "r01c3", by="op-1")
    store.add(f"{group}/{task}/demo_0003", "r01c2", by="op-1")  # a correction
    got = G.collect_segment(records, lay, 1, confirmations=store.latest())
    cards = {r.number: (r.card, r.card_source, r.pairable) for r in got["rows"]}
    assert cards[1] == ("r01c1", "operator_confirmed", True)
    assert cards[3] == ("r01c2", "operator_confirmed", True)
    assert cards[5] == (None, "unconfirmed", False)
    assert [p["candidate_card"] for p in got["pending"]] == ["r01c3"]
    fact = got["facts"]["run1"][2]
    assert fact.ended_by == "operator_stop" and fact.stop_reason == "operator_stop"
    assert fact.labels == {"operator_label": "failure", "autonomous_verdict": "failure"}
    assert fact.steps == 120 and fact.max_steps == 400


def test_a_segment_with_two_arm_codes_is_refused(tmp_path):
    lay = fx.layout(per_arm=5, segment_trials=5)
    write_rollout(
        tmp_path, "g", "t", 1, note=G.eval_note(fx.CAMPAIGN, 1, "X1"), run_id="r1"
    )
    write_rollout(
        tmp_path, "g", "t", 2, note=G.eval_note(fx.CAMPAIGN, 1, "X2"), run_id="r2"
    )
    with pytest.raises(G.GuidedError, match="several arm codes"):
        G.collect_segment(G.scan_rollouts(tmp_path, "g", "t"), lay, 1)
    with pytest.raises(G.GuidedError, match="not in the campaign"):
        G.collect_segment([], lay, 99)


def test_the_online_judgement_maps_to_verdict_kinds(tmp_path):
    note = "c1 s01"
    write_rollout(
        tmp_path, "g", "t", 1, note=note, run_id="r", agent="failure", undecided=True
    )
    write_rollout(tmp_path, "g", "t", 2, note=note, run_id="r", agent=None)
    path = write_rollout(tmp_path, "g", "t", 3, note=note, run_id="r")
    meta = json.loads((path / "metadata.json").read_text())
    meta["eval"]["agent_label"]["status"] = "timeout"
    (path / "metadata.json").write_text(json.dumps(meta))
    a, b, c = (G.legacy_fact(r) for r in G.scan_rollouts(tmp_path, "g", "t"))
    assert a.labels["autonomous_verdict"] == "undecided"
    assert "autonomous_verdict" not in b.labels
    assert c.labels["autonomous_verdict"] == "none"
    assert (
        G.legacy_fact(G.scan_rollouts(tmp_path, "g", "t")[0], posthoc="failure").labels[
            "posthoc_verdict"
        ]
        == "failure"
    )


def test_scanning_reads_only(tmp_path):
    write_rollout(tmp_path, "g", "t", 1, note="c1 s01", run_id="r")
    (tmp_path / "g" / "t" / "demo_0002").mkdir()  # no metadata yet
    (tmp_path / "g" / "t" / "notes.txt").write_text("x")
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    records = G.scan_rollouts(tmp_path, "g", "t")
    assert [r.number for r in records] == [1]
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
    assert G.scan_rollouts(tmp_path, "g", "missing") == []


def test_agreement_is_reported_apart_by_how_the_episode_ended(tmp_path):
    note = "c1 s01"
    write_rollout(
        tmp_path,
        "g",
        "t",
        1,
        note=note,
        run_id="r",
        operator="success",
        agent="success",
    )
    write_rollout(
        tmp_path,
        "g",
        "t",
        2,
        note=note,
        run_id="r",
        operator="failure",
        agent="success",
    )
    write_rollout(
        tmp_path,
        "g",
        "t",
        3,
        note=note,
        run_id="r",
        operator="success",
        agent="failure",
        ended_by="operator_key",
    )
    facts = [G.legacy_fact(r) for r in G.scan_rollouts(tmp_path, "g", "t")]
    got = G.segment_agreement(facts)
    assert got["carries_over"] == "budget"
    assert got["all"]["judged"] == 3 and got["all"]["agreement"]["k"] == 1
    assert got["budget"]["judged"] == 2 and got["budget"]["agreement"]["k"] == 1
    assert (
        got["operator_stop"]["judged"] == 1
        and got["operator_stop"]["agreement"]["k"] == 0
    )
    assert "unknown" not in got


# ------------------------------------------------------------ confirmations


def test_card_confirmations_append_and_the_last_wins(tmp_path):
    store = G.CardConfirmations(tmp_path / "cards.jsonl")
    store.add("g/t/demo_0001", "r01c1", by="op-1")
    store.add("g/t/demo_0002", "r01c2", by="op-1")
    store.add("g/t/demo_0001", "r01c3", by="op-2")
    store.add("g/t/demo_0002", None, by="op-2")  # withdrawn
    assert store.latest() == {"g/t/demo_0001": "r01c3"}
    assert len(store.lines()) == 4  # earlier lines stay on file
    with pytest.raises(G.GuidedError, match="principal"):
        store.add("g/t/demo_0003", "r01c1", by="Jane Doe <jane@example.org>")
    with pytest.raises(L.LedgerError):
        store.add("g/t/demo_0003", "../x", by="op-1")
    # A write cut short is no line, and the next one starts clean.
    with (tmp_path / "cards.jsonl").open("ab") as handle:
        handle.write(b'{"schema": "levi.aeri.campaign')
    assert store.latest() == {"g/t/demo_0001": "r01c3"}
    store.add("g/t/demo_0004", "r01c4", by="op-1")
    assert store.latest()["g/t/demo_0004"] == "r01c4"
    (tmp_path / "cards.jsonl").write_text('{"schema": "other"}\n')
    with pytest.raises(G.GuidedError, match="line 1"):
        store.latest()


# --------------------------------------------------------------- end to end


def test_a_guided_campaign_reaches_a_report(tmp_path):
    """Two arms x 10 cards on the legacy client: commands, rollouts,
    confirmations, ledger, report on the operator labels."""
    lay = fx.layout(per_arm=10, segment_trials=5)
    groups = {"A": "pi05_fr3_all_step49999", "B": "recap_cfg_r2_best_step14300_jax"}
    cmds = G.segment_commands(base(), lay, groups)
    root = tmp_path / "rollouts"
    store = G.CardConfirmations(tmp_path / "campaign" / "cards.jsonl")
    number = 0
    for seg, cmd in zip(lay.segments, cmds, strict=True):
        for j, card in enumerate(seg.cards, start=1):
            number += 1
            ok = (j % 2 == 0) if seg.arm == "A" else (j != 3)
            write_rollout(
                root,
                groups[seg.arm],
                "pick",
                number,
                note=cmd["eval_note"],
                run_id=f"run{seg.segment}",
                operator="success" if ok else "failure",
                agent="success" if ok else "failure",
                ended_by="budget" if j % 2 else "operator_key",
                created=f"2026-10-10T10:{number:02d}:00+0800",
            )
            store.add(f"{groups[seg.arm]}/pick/demo_{number:04d}", card, by="op-1")
    records = [r for g in groups.values() for r in G.scan_rollouts(root, g, "pick")]
    collected = [
        G.collect_segment(records, lay, s.segment, confirmations=store.latest())
        for s in lay.segments
    ]
    assert all(not c["pending"] for c in collected)
    bound = G.bind_runs(lay, collected)
    facts = {run: fs for c in collected for run, fs in c["facts"].items()}
    led = L.derive(bound, facts)
    assert all(r.pairable for r in led.rows) and len(led.rows) == 20
    info = R.CampaignInfo(
        campaign_id=fx.CAMPAIGN,
        arms=(
            R.ArmInfo("A", checkpoint=groups["A"]),
            R.ArmInfo("B", checkpoint=groups["B"]),
        ),
        trials_per_arm=10,
        comparison=("B", "A"),
    )
    manifest = write_report(
        tmp_path / "report", led, info, "operator_label", layout=bound, now=0
    )
    analysis = json.loads(
        (tmp_path / "report/operator_label/data/analysis.json").read_text()
    )
    assert analysis["comparisons"][0]["n_pairs"] == 10
    assert set(analysis["agreement"]["A"]) == {"all", "budget", "operator_stop"}
    assert manifest["conclusion_level"] == "exploratory"
    for path in (tmp_path / "report").rglob("*"):
        if path.is_file():
            assert b"222222222222" not in path.read_bytes()
            assert b"robot-pc" not in path.read_bytes()
