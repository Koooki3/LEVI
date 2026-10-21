"""Mutation check of the gates of the front end of Step 5 (the autonomy system): each
gate is removed (or one safety property turned round) in turn, the test files meant
to guard it run, and every mutation must make at least one of them fail. A mutation
that survives means a gate nobody tests.

    uv run --no-sync python tests/marvel0/mutate_autonomy_ui.py [--check] [--no-baseline] [name ...]

``--check`` only verifies that every mutation applies (its old text occurs exactly
once) and runs nothing; ``--no-baseline`` skips the run of each test file on the
unbroken copy (the first thing a sweep does). The mutations are made in a **copy**
of ``src/``, ``levi/marvel0/``, ``tests/marvel0/fixtures/`` and the configuration
in a temporary folder (``node_modules`` is linked), so the checkout itself is never
edited and nobody else's test run can meet a broken gate; the tests run there with
bun, one file per process, under ``nice -n 19 taskset -c 20-23``. It refuses to run
while a robot process is alive (the passive ``ps`` check of setup.md): a sweep is
many test runs, and the arm's control loop is not to be disturbed by it;
``--even-if-robot-runs`` lifts that (one file at a time stays). The bun used is the
one of the LEVI checkout (``.runtime/bun-linux-x64``) unless ``BUN`` names another.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="mutate-autonomy-ui-"))
COPIED = ("src", "levi/marvel0", "tests/marvel0/fixtures")
CONFIG = ("package.json", "tsconfig.json", "tsconfig.test.json", "bunfig.toml")
ROBOT = re.compile(
    r"ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)
BUN = os.environ.get("BUN") or str(
    Path(__file__).resolve().parents[5] / ".runtime" / "bun-linux-x64" / "bun"
)
if not Path(BUN).exists():
    BUN = shutil.which("bun") or "bun"

M = "src/components/marvel0/"
T = "src/components/marvel0/__tests__/"
LOGIC = T + "autonomy-logic.test.ts"
DOC = T + "autonomy-doc.test.ts"
CONTRACT = T + "autonomy-contract.test.ts"
CATALOG = T + "autonomy-catalog.test.ts"
STYLES = T + "autonomy-styles.test.ts"
RUN = T + "autonomy-run.test.tsx"
ACTIONS = T + "autonomy-actions.test.tsx"
PREPARE = T + "autonomy-prepare.test.tsx"
PAGE = T + "autonomy-page.test.tsx"

# (name, file, old text that occurs exactly once, new text, test files)
MUTATIONS = [
    # --- the delays and gaps of a key ------------------------------------------------
    (
        "delay: a key that appears is clickable at once",
        M + "autonomy-logic.ts",
        """    first === undefined
      ? limits.armDelayMs
      : first + limits.armDelayMs - ctx.now,""",
        "    0,",
        (LOGIC, RUN),
    ),
    (
        "delay: the backend's not_before is ignored",
        M + "autonomy-logic.ts",
        "      : Math.min(key.notBefore - ctx.now, limits.keyGapMs),",
        "      : 0,",
        (LOGIC,),
    ),
    (
        "delay: a far-off clock keeps a key off for as long as it says",
        M + "autonomy-logic.ts",
        "      : Math.min(key.notBefore - ctx.now, limits.keyGapMs),",
        "      : key.notBefore - ctx.now,",
        (LOGIC,),
    ),
    (
        "gap: a key may follow the last one at once",
        M + "autonomy-logic.ts",
        "ctx.lastKeyAt === null ? 0 : ctx.lastKeyAt + limits.keyGapMs - ctx.now,",
        "0,",
        (LOGIC, RUN),
    ),
    (
        "delay: the constant is 150 ms, not 1.5 s",
        M + "autonomy-logic.ts",
        "armDelayMs: 1_500,",
        "armDelayMs: 150,",
        (LOGIC, CONTRACT),
    ),
    (
        "gap: the constant is 2 s, not 3 s",
        M + "autonomy-logic.ts",
        "keyGapMs: 3_000,",
        "keyGapMs: 2_000,",
        (LOGIC, CONTRACT),
    ),
    (
        "stale: a read of any age keeps the keys on",
        M + "autonomy-logic.ts",
        'if (!ctx.current || age > limits.docStaleMs) return off(local("doc_stale"));',
        "",
        (LOGIC, RUN, ACTIONS),
    ),
    (
        "keys: a key the backend does not offer can be clicked",
        M + "autonomy-logic.ts",
        "  if (!key.allowed)\n    return off(",
        "  if (false)\n    return off(",
        (LOGIC, RUN),
    ),
    # --- one key per prompt ----------------------------------------------------------
    (
        "one key per prompt: the other keys stay on after one was sent",
        M + "autonomy-logic.ts",
        "  if (phase !== null)\n    return ctx.sent?.action === key.id",
        "  if (phase !== null && ctx.sent?.action === key.id)\n    return ctx.sent?.action === key.id",
        (LOGIC, RUN),
    ),
    (
        "one key per prompt: a key never turns 'not seen' after 5 s",
        M + "autonomy-logic.ts",
        'return now - sent.at >= limits.sentUnseenMs ? "unseen" : "sent";',
        'return "sent";',
        (LOGIC, RUN),
    ),
    (
        "one key per prompt: a lock outlives the prompt it was for",
        M + "autonomy-logic.ts",
        "if (!sent || sent.runId !== run.runId || sent.seq !== run.state.seq)",
        "if (!sent || sent.runId !== run.runId)",
        (LOGIC, RUN),
    ),
    (
        "double click: a second key request goes out while one is on its way",
        M + "use-autonomy.ts",
        "      if (busy.current) return false;\n      busy.current = true;\n      setWorking(key);",
        "      busy.current = true;\n      setWorking(key);",
        (RUN, PREPARE),
    ),
    (
        "sent: the page forgets the key it sent",
        M + "use-autonomy.ts",
        "            ids.release();\n            setSent(record);\n            setLastKeyAt(Date.now());\n            return true;",
        "            ids.release();\n            setLastKeyAt(Date.now());\n            return true;",
        (RUN,),
    ),
    (
        "sent: a request with no answer is not held as possibly sent",
        M + "use-autonomy.ts",
        "            setSent({ ...record, lost: true });",
        "            setLastKeyAt(Date.now());",
        (RUN,),
    ),
    (
        "seen: a key is sent with no prompt in the document",
        M + "use-autonomy.ts",
        "if (!runId || !key.token || promptId === null) {",
        "if (!runId || !key.token) {",
        (RUN,),
    ),
    (
        "phrase: a level C key is sent without its phrase",
        M + "use-autonomy.ts",
        'key.level === "C" ? phrase : undefined,',
        "undefined,",
        (ACTIONS,),
    ),
    # --- windows ---------------------------------------------------------------------
    (
        "window: a level B key is sent by the click, with no window",
        M + "autonomy-keys.tsx",
        '    if (key.level === "A") {',
        "    if (true) {",
        (RUN, ACTIONS, PREPARE),
    ),
    (
        "window: a key that is not on can be clicked",
        M + "autonomy-keys.tsx",
        'const clickable = vm.mode === "ready";',
        'const clickable = vm.mode !== "locked";',
        (RUN, ACTIONS),
    ),
    (
        "window: a window whose prompt moved on stays open and sends the new prompt's key",
        M + "autonomy-keys.tsx",
        "    asking !== null &&\n    (run.runId !== asking.runId || run.state.seq !== asking.seq);",
        "    false;",
        (RUN,),
    ),
    (
        "window: Confirm is on whatever the key's state",
        M + "autonomy-keys.tsx",
        'const ready = vm?.mode === "ready" && actions.working === null;',
        "const ready = true;",
        (RUN,),
    ),
    # --- Stop ------------------------------------------------------------------------
    (
        "Stop: a second Stop is sent after the first (no single flight)",
        M + "use-autonomy.ts",
        "    if (stopBusy.current) return false;\n    const seen",
        "    const seen",
        (ACTIONS,),
    ),
    (
        "Stop: it waits behind a key on its way",
        M + "use-autonomy.ts",
        "    if (stopBusy.current) return false;\n    const seen",
        "    if (stopBusy.current || busy.current) return false;\n    const seen",
        (ACTIONS,),
    ),
    (
        "Stop: sent once, it is offered again on the same phase",
        M + "autonomy-logic.ts",
        "return { mode, enabled: seen.allowed && !lockedHere };",
        "return { mode, enabled: seen.allowed };",
        (LOGIC, ACTIONS),
    ),
    (
        "Stop: shown when D is gone",
        M + "autonomy-logic.ts",
        'if (!seen || !seen.alive) return { mode: "hidden", enabled: false };',
        'if (!seen) return { mode: "hidden", enabled: false };',
        (LOGIC, ACTIONS),
    ),
    (
        "Stop: the card's button is on where the backend does not offer it",
        M + "autonomy-run-card.tsx",
        "        disabled={!enabled}\n        onClick={() => void actions.stop()}",
        "        onClick={() => void actions.stop()}",
        (ACTIONS,),
    ),
    (
        "Stop: the dock's button is on where the backend does not offer it",
        M + "autonomy-step.tsx",
        "        disabled={!enabled}\n        loading={actions.stopping}",
        "        loading={actions.stopping}",
        (ACTIONS,),
    ),
    # --- presence --------------------------------------------------------------------
    (
        "presence: a tab out of focus or hidden still reports",
        M + "use-autonomy.ts",
        """    const looking = () =>
      document.visibilityState !== "hidden" &&
      (typeof document.hasFocus !== "function" || document.hasFocus());""",
        "    const looking = () => true;",
        (ACTIONS,),
    ),
    (
        "presence: it reports with no D alive",
        M + "use-autonomy.ts",
        "  useEffect(() => {\n    if (!active) return;\n    let timer",
        "  useEffect(() => {\n    let timer",
        (ACTIONS,),
    ),
    (
        "delay: a key counts from the epoch, so it is never new",
        M + "use-autonomy.ts",
        "if (!map.has(id)) map.set(id, Date.now());",
        "if (!map.has(id)) map.set(id, 0);",
        (RUN,),
    ),
    # --- what is shown ---------------------------------------------------------------
    (
        "blind rule: the run's own page is linked before the run ended",
        M + "autonomy-logic.ts",
        'if (!run || !run.name || run.state.id !== "ended" || run.d.alive) return null;',
        "if (!run || !run.name) return null;",
        (LOGIC, ACTIONS),
    ),
    (
        "homing: a profile without startup homing shows o, p and h",
        M + "autonomy-logic.ts",
        'if (homeless && key.id.startsWith("home_")) return false;',
        "",
        (LOGIC, ACTIONS),
    ),
    (
        "keys: every key of the document is shown, whatever the state",
        M + "autonomy-logic.ts",
        'return key.allowed || key.whyCode !== "state_not_allowed";',
        "return true;",
        (LOGIC, RUN),
    ),
    (
        "document: a key whose byte is not the page's label is offered",
        M + "autonomy-doc.ts",
        "!id || !level || !motion || !slot || !byte || byte !== KEY_BYTE[id]",
        "!id || !level || !motion || !slot || !byte",
        (DOC,),
    ),
    (
        "document: a state the page does not know is shown as idle",
        M + "autonomy-doc.ts",
        'id: stateId ?? "unknown",',
        'id: stateId ?? "idle",',
        (DOC,),
    ),
    (
        "document: a prepared run that does not say it is intact is intact",
        M + "autonomy-doc.ts",
        "intact: bool(item.intact) === true,",
        "intact: bool(item.intact) !== false,",
        (DOC, LOGIC),
    ),
    (
        "document: a readiness row with an unknown level is met",
        M + "autonomy-doc.ts",
        'level: oneOf(READY_LEVELS, item.level) ?? "unknown",',
        'level: oneOf(READY_LEVELS, item.level) ?? "ok",',
        (DOC,),
    ),
    (
        "document: a todo that is a bare code name is shown as words",
        M + "autonomy-doc.ts",
        '!/^[a-z0-9_]+$/.test(text) ? text : null',
        "text",
        (ACTIONS,),
    ),
    (
        "ended: a signal with no exit code says nothing",
        M + "autonomy-logic.ts",
        'if (code === null) return signal === null ? null : "signal";',
        "if (code === null) return null;",
        (LOGIC, ACTIONS),
    ),
    (
        "start: an unknown readiness row does not refuse",
        M + "autonomy-logic.ts",
        'rows.filter((row) => row.level === "refuse" || row.level === "unknown");',
        'rows.filter((row) => row.level === "refuse");',
        (LOGIC, PREPARE),
    ),
    (
        "start: a prepared run that changed can be started",
        M + "autonomy-logic.ts",
        'else if (!ready.prepared.intact) out.push(own("prepared_changed"));',
        "",
        (LOGIC, PREPARE),
    ),
    (
        "start: the evaluation client of step 4 does not matter",
        M + "autonomy-logic.ts",
        'if (ctx.evalLive) out.push(own("eval_live"));',
        "",
        (LOGIC, PREPARE),
    ),
    (
        "start: the page does not hand Step 5 whether the client is live",
        "src/app/marvel0/page.tsx",
        "<AutonomyStep evalLive={ownRunLive} />",
        "<AutonomyStep evalLive={false} />",
        (PAGE,),
    ),
    (
        "form: any task is a task",
        M + "autonomy-logic.ts",
        "  if (!pattern.test(task) || !/[a-z]/.test(task))",
        "  if (false)",
        (LOGIC, PREPARE),
    ),
    (
        "start: the phrase is not the backend's template",
        M + "autonomy-logic.ts",
        '  template = "start aeri {name}",\n): string => template.replace("{name}", name);',
        '  template = "start aeri {name}",\n): string => template;',
        (LOGIC, PREPARE),
    ),
    (
        "execute: the long variant has no suffix",
        M + "autonomy-logic.ts",
        "? `${base} after policy switch check`",
        "? base",
        (LOGIC, ACTIONS),
    ),
    (
        "hosting: the page acts although the control file switched it off",
        M + "autonomy-step.tsx",
        "    ready.value?.enabled === false ||\n",
        "    false ||\n",
        (ACTIONS,),
    ),
    (
        "honest wording: the fixed sentence is left out of the step",
        M + "autonomy-step.tsx",
        '            {t("marvel0.autonomy.honest")}',
        '            {""}',
        (ACTIONS, CATALOG),
    ),
    (
        "force-end: its fold is open although D can still wrap up",
        M + "autonomy-run-card.tsx",
        "open={run.end.allowed || undefined}",
        "open={true}",
        (ACTIONS,),
    ),
    # --- the contract with the backend -------------------------------------------------
    (
        "contract: the key body carries a field the backend's model does not have",
        M + "autonomy-api.ts",
        "  token,\n  tab_id: tabId,",
        "  token,\n  raw_byte: 'q',\n  tab_id: tabId,",
        (CONTRACT,),
    ),
    (
        "contract: the prepare body drops the gripper wait",
        M + "autonomy-logic.ts",
        "    gripper_confirm_s:\n      form.gripperConfirmS.trim() === \"\"\n        ? null\n        : Number(form.gripperConfirmS.trim()),\n",
        "",
        (CONTRACT, LOGIC),
    ),
    (
        "contract: a route of the page goes to another path",
        M + "autonomy-api.ts",
        'run: (signal) => call("aeri/run", limit.read, undefined, signal),',
        'run: (signal) => call("aeri/runs", limit.read, undefined, signal),',
        (CONTRACT,),
    ),
    (
        "contract: the page's key label disagrees with the backend's byte",
        M + "autonomy-doc.ts",
        '  inventory_yes: "y",',
        '  inventory_yes: "Y",',
        (CONTRACT, DOC),
    ),
    # --- words and look ------------------------------------------------------------------
    (
        "words: a sentence of a refusal code is missing from the Chinese",
        "src/i18n/zh.json",
        '  "marvel0.autonomy.err.key_sent": "这个提示已经发过键；请等终端 D 的下一个提示。",\n',
        "",
        (CATALOG,),
    ),
    (
        "words: the Chinese of a state has a Latin letter in its headline",
        "src/i18n/zh.json",
        '"marvel0.autonomy.run.title": "托管的终端",',
        '"marvel0.autonomy.run.title": "托管的终端 D",',
        (CATALOG,),
    ),
    (
        "look: Stop is not 64 px high",
        M + "autonomy-styles.css",
        "  min-height: 64px;",
        "  min-height: 32px;",
        (STYLES,),
    ),
    (
        "look: a colour is written out",
        M + "autonomy-styles.css",
        "  border: 1px solid var(--ds-danger);\n  border-radius: var(--ds-radius-md);\n  background: var(--ds-danger-bg);",
        "  border: 1px solid #ff0000;\n  border-radius: var(--ds-radius-md);\n  background: var(--ds-danger-bg);",
        (STYLES,),
    ),
]


def robot_running() -> bool:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return any(ROBOT.search(line) for line in out.splitlines())


def prepare_copy() -> None:
    for rel in COPIED:
        shutil.copytree(
            SOURCE / rel,
            ROOT / rel,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in CONFIG:
        if (SOURCE / name).exists():
            shutil.copy2(SOURCE / name, ROOT / name)
    (ROOT / "node_modules").symlink_to(SOURCE / "node_modules")


def run_test(test: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["nice", "-n", "19", "taskset", "-c", "20-23", BUN, "test", test],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        # A gate removed so that a test waits for something that never comes
        # is a gate the test notices (it does not pass); a hang counts as caught.
        return 124, "timed out"
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    args = sys.argv[1:]
    check = "--check" in args
    baseline = "--no-baseline" not in args
    even = "--even-if-robot-runs" in args
    names = [a for a in args if not a.startswith("--")]
    chosen = [m for m in MUTATIONS if not names or any(n in m[0] for n in names)]
    if not even and not check and robot_running():
        print("a robot process is alive: not running a sweep (--even-if-robot-runs)")
        return 2
    prepare_copy()
    survivors, applied = [], 0
    try:
        for name, rel, old, new, tests in chosen:
            path = ROOT / rel
            text = path.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"CANNOT APPLY  {name}: {text.count(old)} matches of the old text in {rel}")
                survivors.append(name)
                continue
            applied += 1
            if check:
                continue
        if check:
            print(f"{applied} of {len(chosen)} mutations apply")
            return 0 if applied == len(chosen) else 1
        if baseline:
            for test in sorted({t for m in chosen for t in m[4]}):
                code, out = run_test(test)
                print(f"baseline {test}: {'ok' if code == 0 else 'FAILS'}")
                if code != 0:
                    print(out[-2000:])
                    return 1
        for name, rel, old, new, tests in chosen:
            path = ROOT / rel
            original = path.read_text(encoding="utf-8")
            if original.count(old) != 1:
                continue
            path.write_text(original.replace(old, new), encoding="utf-8")
            try:
                caught = []
                for test in tests:
                    code, out = run_test(test)
                    if code != 0:
                        caught.append(Path(test).name)
                        break
                print(f"{'caught  ' if caught else 'SURVIVED'}  {name}" + (f"  ({caught[0]})" if caught else ""))
                if not caught:
                    survivors.append(name)
            finally:
                path.write_text(original, encoding="utf-8")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"{len(chosen) - len(survivors)} of {len(chosen)} mutations caught")
    for name in survivors:
        print("  survived:", name)
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
