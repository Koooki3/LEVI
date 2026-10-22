"""Writes ``levi/marvel0/aeri_prompts.json``: the words and the key tables of
terminal D (``levi/automatic/console_text.py``, ``console.py``) for each AERI
code version the page knows (a "profile"), copied from the real sources with
``git show`` and the ``ast`` module (nothing of AERI is imported or run).

    uv run --no-sync python tests/marvel0/regen_aeri_prompts.py [--check]

``--check`` only compares what this would write with the committed file.
The page reads the JSON (``levi/marvel0/aeri_screen.py``): it tells terminal D's
prompts apart by these templates. ``scripts/check_aeri_prompts.py`` checks a
worktree against the same file; ``tests/marvel0/fake_aeri.py`` prints these
templates. So a word that D changes is found in one place, and a profile
that differs from the code it names cannot be hosted (the page refuses an
unknown version rather than guessing).

The profiles (decision 2026-10-21): ``harvest-5bc750ca`` is the default, the
code of the real run of 18 Oct (``r08``); ``bfdee2cf`` is the reviewed pin of
AERI.md (it has no startup home, no forward progress line, no reverse ``m``
hint); ``main-297a74c3`` is main's AERI code (harvest plus the ``camera_stale``
wait and the early-check pause line)."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from check_aeri_prompts import assignments

OUT = ROOT / "levi" / "marvel0" / "aeri_prompts.json"
# The maintainer's checkout (it holds lab/worktrees/); elsewhere, such as on CI,
# this repository itself: the profiles' worktrees are then absent and the tests
# that need them skip, but importing this module must not fail.
LAB = next(
    (p for p in Path(__file__).resolve().parents if (p / "lab" / "worktrees").is_dir()),
    ROOT,
)
PROFILES = {
    "harvest-5bc750ca": {
        "repo": LAB / "lab" / "worktrees" / "harvest-validation-fix",
        "commit": "5bc750ca",
        "title": "The code of the real run of 18 Oct (harvest-validation-fix)",
    },
    "bfdee2cf": {
        "repo": LAB / "lab" / "worktrees" / "aeri-subtask-loop",
        "commit": "bfdee2cf",
        "title": "The reviewed pin of AERI.md (aeri-subtask-loop)",
    },
    "main-297a74c3": {
        "repo": LAB,
        "commit": "297a74c3",
        "title": "main's AERI code (297a74c3)",
    },
}
SOURCES = (
    "levi/automatic/console_text.py",
    "levi/automatic/console.py",
    "levi/automatic/subtask_units.py",
    "levi/automatic/fr3_run.py",
)
# Words D never prints during a hosted subtask run (provider scene questions,
# the label card, the old human-assisted path).
SKIP_PREFIXES = ("scene_", "label_", "legacy_", "target_", "plain_", "v_")
# Literal tables of console.py the page needs.
TABLES = (
    "ENTER",
    "QUIT",
    "VERDICT_KEYS",
    "CONFIRM_KEYS",
    "CANCEL_KEYS",
    "WAIT_REASONS",
    "SUBTASK_WAIT_NOTES",
    "CONFIRM_REASONS",
    "ROUTINE_WAITS",
)
# The wait reasons whose Enter cannot resume (the text asks to end the run).
NO_RESUME = ("startup_home_not_executed",)
# The prompt texts that ask the person to put the scene or the arm right first.
RESTORE_TODOS = (
    "todo_reset_enter",
    "todo_reset_resume",
    "todo_home_resume",
    "todo_home_pose_resume",
    "todo_subtask_restart",
    "todo_subtask_restart_confirm",
)
RESTORE_PHASES = ("sub_wait_forward", "sub_wait_after_forward", "sub_wait_reverse")
# Lines of other programs terminal D's output holds (r08's recording): they do
# not close a prompt and they are not read as state.
FOREIGN = [
    r"^\s*server config: ",
    r"^\s*NOTE: ",
    r"^Homing over \d+ steps",
    r"^Home reached\.",
    r"^Home verified: ",
    r"^Gripper reply confirmed: ",
    r"^warning: ",
    r"^hostchild: ",
    r"^={20,}$",
    r"^-{20,}$",
    r"^Traceback \(most recent call last\)",
    r'^\s+File "',
]
# Source snippets the drift check looks for (file, substring): the key tables
# and the order of the prompts' own code.
CODE = (
    ("levi/automatic/console.py", 'ENTER = ("\\n", "\\r")'),
    ("levi/automatic/console.py", 'QUIT = ("q", "Q", "\\x04")'),
    (
        "levi/automatic/console.py",
        'VERDICT_KEYS = {"s": "success", "f": "failure", "d": "discard"}',
    ),
    ("levi/automatic/console.py", 'CONFIRM_KEYS = ("y", "Y")'),
    ("levi/automatic/console.py", 'CANCEL_KEYS = ("n", "N")'),
    (
        "levi/automatic/console.py",
        "self.keys.flush()  # a stale Enter must not release a round",
    ),
    ("levi/automatic/fr3_run.py", 'phrase = "execute " + job["config"].run_id'),
    ("levi/automatic/fr3_run.py", 'phrase += " after policy switch check"'),
    ("levi/automatic/fr3_run.py", "if not sys.stdin.isatty():"),
)
CODE_START_HOME = (
    ("levi/automatic/console.py", '("object_handled", "o")'),
    ("levi/automatic/console.py", '("path_clear", "p")'),
    ("levi/automatic/console.py", '("gripper_will_open", "h")'),
)
CAPABILITIES = {
    "harvest-5bc750ca": {
        "start_home": True,
        "progress_line": True,
        "reverse_m": True,
        "camera_stale": False,
        "early_check": False,
    },
    "bfdee2cf": {
        "start_home": False,
        "progress_line": False,
        "reverse_m": False,
        "camera_stale": False,
        "early_check": False,
    },
    "main-297a74c3": {
        "start_home": True,
        "progress_line": True,
        "reverse_m": True,
        "camera_stale": True,
        "early_check": True,
    },
}


def show(repo: Path, commit: str, path: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        check=True,
    )
    return done.stdout.decode("utf-8")


def profile(name: str, spec: dict) -> dict:
    repo, commit = spec["repo"], spec["commit"]
    sources = {path: show(repo, commit, path) for path in SOURCES}
    text = assignments(sources[SOURCES[0]], {"TEXT"})["TEXT"]
    tables = assignments(sources[SOURCES[1]], set(TABLES))
    missing = [t for t in TABLES if t not in tables]
    if missing:
        raise SystemExit(f"{name}: console.py has no table {missing}")
    texts = {
        lang: {k: v for k, v in table.items() if not k.startswith(SKIP_PREFIXES)}
        for lang, table in text.items()
    }
    caps = CAPABILITIES[name]
    code = [{"file": f, "source": s} for f, s in CODE]
    if caps["start_home"]:
        code += [{"file": f, "source": s} for f, s in CODE_START_HOME]
    for entry in code:
        if entry["source"] not in sources[entry["file"]]:
            raise SystemExit(f"{name}: {entry['source']!r} is not in {entry['file']}")
    wait_reasons = tables["WAIT_REASONS"]
    return {
        "title": spec["title"],
        "commit": commit,
        "capabilities": caps,
        "keys": {
            "enter": list(tables["ENTER"]),
            "quit": list(tables["QUIT"]),
            "verdict": tables["VERDICT_KEYS"],
            "confirm": list(tables["CONFIRM_KEYS"]),
            "cancel": list(tables["CANCEL_KEYS"]),
        },
        "wait_reasons": {k: list(v) for k, v in sorted(wait_reasons.items())},
        "wait_notes": tables["SUBTASK_WAIT_NOTES"],
        "confirm_reasons": tables["CONFIRM_REASONS"],
        "routine_waits": tables["ROUTINE_WAITS"],
        "no_resume_reasons": [r for r in NO_RESUME if r in wait_reasons],
        "restore_todos": list(RESTORE_TODOS),
        "restore_phases": list(RESTORE_PHASES),
        "texts": texts,
        "code": code,
        "sources_sha256": {
            path: hashlib.sha256(body.encode()).hexdigest()
            for path, body in sources.items()
        },
    }


def build() -> dict:
    return {
        "schema": "levi.marvel0.aeri_prompts.v1",
        "about": (
            "Terminal D's words and key tables per AERI code version, copied "
            "from the sources by tests/marvel0/regen_aeri_prompts.py; checked "
            "against a worktree by scripts/check_aeri_prompts.py."
        ),
        "default": "harvest-5bc750ca",
        "foreign": FOREIGN,
        "profiles": {name: profile(name, spec) for name, spec in PROFILES.items()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = json.dumps(build(), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("same" if same else f"differs: run this script without --check ({OUT})")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
