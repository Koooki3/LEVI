"""Regenerates the samples of the hosted AERI terminal D that the frontend and the
other backend modules parse as the server's real shape:

* ``src/components/marvel0/__tests__/marvel0-aeri-run.samples.json`` (and its
  twin ``tests/marvel0/fixtures/aeri_run.sample.json``): the exact
  ``levi.marvel0.aeri_run.v1`` documents of ``GET /aeri/run`` in every state the
  page shows (keys, ``why_not``, tokens, Stop, End, notices);
* ``src/components/marvel0/__tests__/marvel0-aeri-run.answers.json``: the answers of
  the other routes (challenge, start, key, stop, end, log) and every refusal
  (``{status, detail}``) the page words.

    uv run --no-sync python tests/marvel0/regen_aeri_run_sample.py

The documents are built in process by the pure functions of
``levi/marvel0/aeri_state.py`` (``derive``, ``offer``, ``document``) from **the
real recording of 18 Oct** (r08: its journal, screen and service steps, cut at
the moments that matter) and facts written out here; the answers by the
supervisor on the scripted machine (a temporary site, a fake child, a fixed
zone; the volatile fields are fixed). No terminal D, process or socket is started
and nothing of the real machine is looked at.
``tests/marvel0/test_marvel0_aeri_sample.py`` fails, naming this command, when a
committed file differs from what this builds.

Keep this file in step with the document: a field added to ``aeri_state.document``
shows up here by regenerating; a state added to ``aeri_state.STATES`` fails the
test until it has a sample."""

import json
import tempfile
import time
import unicodedata
from pathlib import Path

from regen_sample import FOLDER, ROOT, fixed_zone

from levi.marvel0 import aeri_journal as jr
from levi.marvel0 import aeri_screen as sc
from levi.marvel0 import aeri_state as st

SAMPLE = FOLDER / "marvel0-aeri-run.samples.json"
ANSWERS = FOLDER / "marvel0-aeri-run.answers.json"
TWIN = ROOT / "tests" / "marvel0" / "fixtures" / "aeri_run.sample.json"
CODES = ROOT / "tests" / "marvel0" / "fixtures" / "aeri_codes.json"
FIX = ROOT / "tests" / "marvel0" / "fixtures" / "aeri_r08"
NOW = 1_792_549_865.0  # 2026-10-21 02:31:05 UTC = 10:31:05 +08:00
RUN_ID = "20261021T101500-a1b2c3"
NAME = "aeri-20261021-ext-r01"
SECRET = b"sample-secret---"
PROFILE = st.PROFILES["harvest-5bc750ca"]
RECORD = {
    "schema": st.RECORD_SCHEMA,
    "run_id": RUN_ID,
    "name": NAME,
    "mode": "external",
    "task": "pick the pepper into the white plate",
    "rounds": 5,
    "steps": 200,
    "phase": "started",
    "profile": "harvest-5bc750ca",
    "console_lang": "zh",
    "leader": {"pid": 123450, "identity": {"start_ticks": "1", "boot": "b"}},
    "created_epoch": NOW - 900.0,
}
SCREEN = (FIX / "screen.txt").read_bytes()
JOURNAL = (FIX / "state_journal.jsonl").read_bytes().splitlines(keepends=True)


def cut(marker: str) -> bytes:
    text = SCREEN.decode()
    at = text.index(marker)
    if marker.startswith("请输入"):
        return text[: at + len(marker)].replace("aeri-20261018-ext-r08", NAME).encode()
    end = text.find("\r\n", at)
    return text[: end + 2 if end >= 0 else len(text)].encode()


def phrase(variant=False) -> bytes:
    tail = " after policy switch check" if variant else ""
    return f"请输入 execute {NAME}{tail}：".encode()


class Maker:
    """Facts, one look after another the way the supervisor looks."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.n = 0
        self.lex = sc.Lexicon(PROFILE.data, sc.prompts()["foreign"], "zh")

    def jview(self, upto=None, extra=()):
        self.n += 1
        run = self.folder / f"j{self.n}"
        (run / "subtasks").mkdir(parents=True)
        for side in (FIX / "subtasks").iterdir():
            (run / "subtasks" / side.name).write_bytes(side.read_bytes())
        body = b"".join(JOURNAL[: upto or len(JOURNAL)])
        body += b"".join(json.dumps(r).encode() + b"\n" for r in extra)
        (run / jr.JOURNAL).write_bytes(body)
        return jr.JournalReader(run / jr.JOURNAL, run).poll()

    def screen(self, data):
        buf = sc.ScreenBuf()
        buf.feed(data if isinstance(data, bytes) else data.encode())
        return sc.read(buf, self.lex), buf

    def steps(self, n):
        path = self.folder / f"steps{n}.jsonl"
        path.write_bytes(
            b"".join((FIX / "steps.jsonl").read_bytes().splitlines(keepends=True)[:n])
        )
        reader = jr.StepsReader(path)
        return reader.poll()

    def build(
        self,
        j=None,
        screen=b"",
        *,
        record=None,
        settle=True,
        icanon=False,
        facts=None,
        steps=0,
        tail=None,
        **ctx,
    ) -> dict:
        view, buf = self.screen(screen)
        f = st.Facts(
            now=NOW,
            record=dict(RECORD if record is None else record),
            profile=PROFILE,
            alive=True,
            keyboard=True,
            j=j if j is not None else jr.JView(),
            screen=view,
            runner=jr.RView(present=True, state="running", pid=123456),
            runner_age_s=1.4,
            runner_seen=True,
            steps=self.steps(steps) if steps else jr.SView(),
            icanon=icanon,
            isig=True,
            started_age_s=900.0,
            external=True,
        )
        for key, value in (facts or {}).items():
            setattr(f, key, value)
        mem = st.Memory()
        d = st.derive(f, mem)
        if settle:
            f.now = NOW + 1.2
            d = st.derive(f, d.mem)
            f.now = NOW + 3.0
            d = st.derive(f, d.mem)
        context = st.Context(
            now=f.now,
            secret=SECRET,
            seq=ctx.pop("seq", 57),
            run_id=(f.record or {}).get("run_id"),
            monitor_state=ctx.pop("monitor_state", "ok"),
            monitor_age_s=0.4,
            presence_age_s=ctx.pop("presence_age_s", 2.1),
            history=ctx.pop("history", []),
            locks=ctx.pop("locks", HELD),
            tail=tail if tail is not None else buf.tail(),
            pid=123456 if (f.record or {}).get("phase") else None,
            **ctx,
        )
        return st.document(f, d, context)


HELD = {
    "arm": "held_by_d",
    "gpu": "held_by_d",
    "ports": {"8000": "d_unit", "8100": "d_unit", "8110": "free"},
}
FREE = {
    "arm": "free",
    "gpu": "free",
    "ports": {"8000": "free", "8100": "free", "8110": "free"},
}


def key_row(time_, key, byte, state, prompt, rnd):
    return {
        "time": time_,
        "key": key,
        "byte": byte,
        "state": state,
        "prompt_id": prompt,
        "round": rnd,
    }


H = [
    key_row(
        "2026-10-21T10:30:05+08:00",
        "execute",
        "execute",
        "await_phrase",
        "phrase:612",
        0,
    ),
    key_row(
        "2026-10-21T10:31:48+08:00",
        "inventory_yes",
        "y",
        "await_inventory",
        "inventory:3320",
        0,
    ),
]


def samples() -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        mk = Maker(Path(tmp))

        def put(name, doc):
            out[name] = doc

        # --- no run
        for name, kw in (
            ("idle", {}),
            ("idle_disabled", {"enabled": False, "start_why": st.why("aeri_disabled")}),
            ("idle_blocked", {"start_why": st.why("driven_elsewhere")}),
            # the run record exists and cannot be read: a D may be running
            (
                "idle_record_unreadable",
                {"start_why": st.why("d_active", record="unreadable")},
            ),
        ):
            f = st.Facts(now=NOW)
            d = st.derive(f, st.Memory())
            ctx = st.Context(
                now=NOW, secret=SECRET, seq=1, presence_age_s=2.1, locks=FREE, **kw
            )
            put(name, st.document(f, d, ctx))
        # --- before the journal
        put(
            "launching",
            mk.build(
                None,
                b"",
                icanon=True,
                settle=False,
                locks=FREE,
                facts={
                    "started_age_s": 3.0,
                    "runner_seen": False,
                    "runner": jr.RView(),
                },
            ),
        )
        put("await_phrase", mk.build(None, phrase(), icanon=True, locks=FREE))
        put(
            "await_phrase_variant",
            mk.build(None, phrase(True), icanon=True, locks=FREE),
        )
        put(
            "services",
            mk.build(
                mk.jview(12), cut("6/7 各轮"), icanon=True, steps=9, history=H[:1]
            ),
        )
        # --- the first home
        for name, marker in (
            ("await_home_o", "首轮回位会打开夹爪"),
            ("await_home_p", "检查机械臂返回 Home"),
            ("await_home_h", "确认知道回位会开爪"),
        ):
            put(name, mk.build(mk.jview(14), cut(marker), steps=9, history=H[:1]))
        # --- the object list and the rounds
        put(
            "await_inventory",
            mk.build(mk.jview(25), cut("清单对就按 y 确认"), steps=9, history=H[:1]),
        )
        release = cut("第 1/5 轮：确认场景就绪")
        put("await_release", mk.build(mk.jview(26), release, steps=9, history=H))
        put(
            "await_release_second",
            mk.build(mk.jview(93), cut("第 2/5 轮：确认场景就绪"), steps=9, history=H),
        )
        j26 = mk.jview(26)
        sent = st.derive(_facts_of(mk, j26, release), st.Memory()).prompt["id"]
        put(
            "await_release_key_sent",
            mk.build(j26, release, steps=9, history=H, keyed=frozenset({sent})),
        )
        put(
            "await_release_key_unconfirmed",
            mk.build(
                j26,
                release,
                steps=9,
                history=H,
                unconfirmed=sent,
                seq=58,
            ),
        )
        put(
            "await_release_unsettled",
            mk.build(j26, release, steps=9, history=H, settle=False),
        )
        put(
            "await_release_monitor_red",
            mk.build(j26, release, steps=9, history=H, monitor_state="red"),
        )
        # the control file switched AERI off during the run: every key is off
        put(
            "await_release_disabled",
            mk.build(j26, release, steps=9, history=H, enabled=False, seq=69),
        )
        put(
            "await_release_presence_lost",
            mk.build(
                j26,
                release,
                steps=9,
                history=H,
                presence_age_s=21.0,
            ),
        )
        # --- a round
        fwd = cut("当前正向子任务 步 66/200")
        j34 = mk.jview(34)
        put("forward_running", mk.build(j34, fwd, steps=9, history=H))
        used = mk.jview(
            34,
            extra=[
                {
                    "sequence_no": 34,
                    "record": "note",
                    "note": {
                        "code": "operator_verdict",
                        "detail": '{"label":"success"}',
                    },
                }
            ],
        )
        put(
            "forward_running_verdict_used",
            mk.build(used, fwd, steps=9, history=H, seq=59),
        )
        put(
            "closing",
            mk.build(mk.jview(37), cut("当前正向子任务结束"), steps=9, history=H),
        )
        put(
            "reverse_running",
            mk.build(
                mk.jview(59),
                cut("[第 1/5 轮] reset 策略正在复位环境"),
                steps=9,
                history=H,
            ),
        )
        # --- waits
        from test_marvel0_aeri_screen import wait_text

        def waited(reason):
            return mk.jview(
                21,
                extra=[
                    {
                        "sequence_no": 34,
                        "record": "committed",
                        "from_state": "VERIFY_INITIAL",
                        "to_state": "WAIT_HUMAN",
                        "reason": reason,
                    }
                ],
            )

        put(
            "await_resume",
            mk.build(
                waited("operator_stop"),
                wait_text("harvest-5bc750ca", "operator_stop"),
                steps=9,
                history=H,
            ),
        )
        put(
            "await_resume_direct",
            mk.build(
                waited("subtask_not_released"),
                wait_text("harvest-5bc750ca", "subtask_not_released")
                + "[第 1/5 轮] "
                + PROFILE.data["texts"]["zh"]["sub_wait_forward"].format(n=1)
                + "\r\n",
                steps=9,
                history=H,
            ),
        )
        texts = PROFILE.data["texts"]["zh"]
        put(
            "await_resume_confirm",
            mk.build(
                waited("operator_stop"),
                wait_text("harvest-5bc750ca", "operator_stop")
                + "[第 1/5 轮] "
                + texts["confirm_resume"]
                + "\r\n",
                steps=9,
                history=H,
            ),
        )
        # --- busy (an external answer awaited)
        put(
            "busy_external",
            mk.build(
                mk.jview(21),
                cut("Home verified"),
                steps=9,
                facts={"queue_pending": 1, "queue_oldest_s": 94.0},
            ),
        )
        # --- winding down
        interrupted = (
            fwd.decode()
            + "收到 SIGINT（Ctrl+C）：已停止下发运动指令，正在安全收尾，不会再开始新的一轮。\r\n"
        )
        sent_rec = {**RECORD, "stop_sent_epoch": NOW - 12.0}
        put(
            "stopping",
            mk.build(
                j34,
                interrupted.encode(),
                steps=9,
                history=H,
                record=sent_rec,
                facts={"stop_written": "sent", "stop_age_s": 12.0},
                seq=60,
            ),
        )
        put(
            "stopping_unconfirmed",
            mk.build(
                j34,
                fwd,
                steps=9,
                history=H,
                record=sent_rec,
                facts={"stop_written": "sent", "stop_age_s": 12.0},
                seq=61,
            ),
        )
        put("stalled", _stalled(mk, j34, interrupted.encode(), sent_rec))
        # --- a Ctrl+C that D never answered, and the retry and the End that follow
        put(
            "stopping_retried_end",
            mk.build(
                j34,
                fwd,
                steps=9,
                history=H,
                record={
                    **sent_rec,
                    "stop_retried": True,
                    "stop_retry_epoch": NOW - 75.0,
                },
                facts={
                    "stop_written": "retried",
                    "stop_age_s": 150.0,
                    "stop_last_age_s": 75.0,
                },
                seq=66,
            ),
        )
        # --- a Ctrl+C that came from elsewhere (a terminal, kill, the script): D
        # answered it, this page never sent one and offers none
        put(
            "stopping_acked_elsewhere",
            mk.build(j34, interrupted.encode(), steps=9, history=H, seq=64),
        )
        # --- a run that another live core owns: watched, not driven
        put(
            "unknown_other_core",
            mk.build(
                j34,
                fwd,
                steps=9,
                history=H,
                facts={"foreign": True, "adopted": True, "keyboard": False},
                seq=65,
            ),
        )
        locked = mk.jview(
            34,
            extra=[
                {
                    "sequence_no": 40,
                    "record": "committed",
                    "from_state": "FORWARD_ACTIVE",
                    "to_state": "FAULT_LOCKED",
                    "reason": "policy_error",
                }
            ],
        )
        put(
            "fault_locked",
            mk.build(
                locked, "运行已锁定 FAULT_LOCKED：policy_error\r\n", steps=9, history=H
            ),
        )
        put(
            "orphaned_unconfirmed",
            mk.build(
                mk.jview(26),
                release,
                steps=9,
                history=H,
                record=sent_rec,
                facts={
                    "adopted": True,
                    "keyboard": False,
                    "stop_written": "sent",
                    "stop_age_s": 12.0,
                },
                seq=67,
            ),
        )
        # --- alive and not followed (a record without the site snapshot, and a site
        # file that does not read): no key, Stop is still the page's
        put(
            "unknown_unfollowed",
            mk.build(
                None,
                b"",
                history=H,
                facts={"blind": True, "keyboard": False, "runner": jr.RView()},
                seq=68,
            ),
        )
        put(
            "orphaned",
            mk.build(
                mk.jview(26),
                release,
                steps=9,
                history=H,
                record=sent_rec,
                facts={
                    "adopted": True,
                    "keyboard": False,
                    "stop_written": "sent",
                    "stop_age_s": 3.0,
                },
            ),
        )
        # --- the sources disagree
        drift = _drift(mk, release)
        put("unknown_drift", drift)
        put(
            "unknown_runner_stale",
            mk.build(
                j34,
                fwd,
                steps=9,
                history=H,
                facts={"runner_age_s": 14.0},
            ),
        )
        # --- a wait that was ended from elsewhere than the keyboard (the inbox)
        text = PROFILE.data["texts"]["zh"]
        by_page = "[第 1/5 轮] " + text["st_resumed_by_page"] + "\r\n"
        put(
            "forward_running_inbox_resume",
            mk.build(j34, (by_page + fwd.decode()).encode(), steps=9, history=H),
        )
        # --- over
        done = {**RECORD, "phase": "ended", "exit": {"code": 0, "signal": None}}
        tail = [
            "[第 1/5 轮] 已按停止请求结束：完成 1/5 轮",
            "7/7 收尾：停止 VLM、停止策略、核对显存释放 / 等待",
        ]
        after = {
            "units": [],
            "ports": {"8000": "free", "8100": "free"},
            "swap_clean": True,
        }
        put(
            "ended_done",
            mk.build(
                mk.jview(),
                cut("7/7 收尾"),
                record=done,
                steps=9,
                history=H,
                tail=tail,
                locks=FREE,
                after_exit=after,
                exit={"code": 0, "signal": None},
                facts={
                    "alive": False,
                    "keyboard": False,
                    "exit": {"code": 0, "signal": None},
                    "runner": jr.RView(present=True, state="exited", pid=123456),
                    "runner_age_s": 3.0,
                },
            ),
        )
        refused = {**done, "exit": {"code": 2, "signal": None}}
        put(
            "ended_refused",
            mk.build(
                None,
                b"",
                record=refused,
                tail=["start pose refused: the arm is not at home"],
                locks=FREE,
                after_exit={
                    "units": ["levi-aeri-policy-forward"],
                    "ports": {"8000": "d_unit", "8100": "free"},
                    "swap_clean": False,
                },
                exit={"code": 2, "signal": None},
                facts={
                    "alive": False,
                    "keyboard": False,
                    "exit": {"code": 2, "signal": None},
                },
            ),
        )
        killed = {**done, "exit": {"code": None, "signal": 9}}
        put(
            "ended_hangup",
            mk.build(
                mk.jview(34),
                fwd,
                record=killed,
                history=H,
                locks=FREE,
                after_exit={
                    "units": ["levi-aeri-vlm-8100", "levi-aeri-policy-forward"],
                    "ports": {"8000": "d_unit", "8100": "d_unit"},
                    "swap_clean": False,
                },
                exit={"code": None, "signal": 9},
                facts={
                    "alive": False,
                    "keyboard": False,
                    "exit": {"code": None, "signal": 9},
                },
            ),
        )
    return out


def _facts_of(mk, j, screen) -> st.Facts:
    view, _ = mk.screen(screen)
    return st.Facts(
        now=NOW,
        record=dict(RECORD),
        profile=PROFILE,
        alive=True,
        keyboard=True,
        j=j,
        screen=view,
        runner=jr.RView(present=True, state="running", pid=1),
        runner_age_s=1.0,
        runner_seen=True,
        icanon=False,
        started_age_s=900.0,
        external=True,
    )


def _stalled(mk, j, screen, rec) -> dict:
    """A winding-down run with nothing new for the stall time."""
    _view, buf = mk.screen(screen)
    f = _facts_of(mk, j, screen)
    f.record = dict(rec)
    f.stop_written, f.stop_age_s = "sent", 160.0
    d = st.derive(f, st.Memory())
    f.now = NOW + st.STALL_S + 5
    f.stop_age_s = 165.0
    d = st.derive(f, d.mem)
    f.now += 1.2
    d = st.derive(f, d.mem)
    ctx = st.Context(
        now=f.now,
        secret=SECRET,
        seq=62,
        run_id=RUN_ID,
        presence_age_s=2.1,
        history=H,
        locks=HELD,
        tail=buf.tail(),
        pid=123456,
    )
    return st.document(f, d, ctx)


def _drift(mk, release: bytes) -> dict:
    changed = release.decode().replace("按 Enter 放行", "请按回车放行").encode()
    j = mk.jview(26)
    _view, buf = mk.screen(changed)
    f = _facts_of(mk, j, changed)
    d = st.derive(f, st.Memory())
    f.now = NOW + st.DRIFT_S + 2
    d = st.derive(f, d.mem)
    ctx = st.Context(
        now=f.now,
        secret=SECRET,
        seq=63,
        run_id=RUN_ID,
        presence_age_s=2.1,
        history=H,
        locks=HELD,
        tail=buf.tail(),
        pid=123456,
    )
    return st.document(f, d, ctx)


WIDTH = 80  # prettier's print width


def _cols(text: str) -> int:
    """Columns prettier counts: a wide character (Chinese) is two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _lines(value, indent, head, tail) -> list:
    """``head`` + the value + ``tail`` as lines the way prettier prints JSON (the
    frontend's ``format:check`` reads the files): an object is always open, keys
    sorted; an array of plain values is on one line when it fits in 80 columns
    (wide characters counted twice), else one value per line."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{head}{{}}{tail}"]
        items = sorted(value.items())
        out = [f"{pad}{head}{{"]
        for n, (key, item) in enumerate(items):
            end = "," if n < len(items) - 1 else ""
            out += _lines(item, indent + 2, f"{_scalar(key)}: ", end)
        return [*out, f"{pad}}}{tail}"]
    if isinstance(value, list):
        if not value:
            return [f"{pad}{head}[]{tail}"]
        if not any(isinstance(item, (dict, list)) for item in value):
            one = f"{pad}{head}[{', '.join(_scalar(item) for item in value)}]{tail}"
            if _cols(one) <= WIDTH:
                return [one]
        out = [f"{pad}{head}["]
        for n, item in enumerate(value):
            out += _lines(item, indent + 2, "", "," if n < len(value) - 1 else "")
        return [*out, f"{pad}]{tail}"]
    return [f"{pad}{head}{_scalar(value)}{tail}"]


def _dump(value) -> str:
    """The text prettier would print (the frontend's ``format:check`` reads the
    files): the printer of the deploy sample."""
    return "\n".join(_lines(value, 0, "", "")) + "\n"


def build() -> str:
    with fixed_zone():
        docs = samples()
    return _dump(
        {
            "schema": "levi.marvel0.aeri_run.samples.v1",
            "about": (
                "Documents of GET /api/levi/marvel0/aeri/run built by aeri_state.derive/document from the "
                "real recording of 18 Oct (r08) and facts written in tests/marvel0/regen_aeri_run_sample.py. "
                "Regenerate: uv run --no-sync python tests/marvel0/regen_aeri_run_sample.py"
            ),
            "samples": docs,
        }
    )


# --- the answers of the other routes ---------------------------------------------------------


def answers() -> dict:
    """The answers of challenge, start, key, stop and end, and every refusal."""
    import pytest
    from aeri_machine import AeriMachine

    out: dict = {}
    mp = pytest.MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmp, fixed_zone():
            m = AeriMachine(tmp, mp)
            m.prepare(rounds=5, steps=200)
            m.present()
            c = m.sup.challenge({"purpose": "start", "name": NAME_OF_MACHINE})
            out["challenge"] = _fix(c)
            body = {
                "request_id": m.rid(),
                "name": NAME_OF_MACHINE,
                "challenge": c["challenge"],
                "confirm": f"start aeri {NAME_OF_MACHINE}",
                "attest": {k: True for k in st.ATTEST},
            }
            out["start"] = _fix(m.sup.start(body))
            m.script(journal=26, screen="第 1/5 轮：确认场景就绪", cbreak=True, steps=9)
            time.sleep(0.05)
            m.sup._cache = None
            view = m.wait_key("release_round")
            out["key"] = _fix(m.sup.key(m.body_for(view, "release_round")))
            out["stop"] = _fix(m.stop())
            out["log"] = _fix(m.sup.log(5))
            m.close()
    finally:
        mp.undo()
    refusals = {}
    from levi.marvel0 import aeri_run as run_module

    for code in st.REFUSALS:
        exc = run_module.http(st.Refuse(code, **EXTRA.get(code, {})))
        refusals[code] = {"status": exc.status_code, "detail": exc.detail}
    return {"answers": out, "refusals": refusals}


NAME_OF_MACHINE = "aeri-20261021-ext-r01"
EXTRA = {
    "confirm_required": {"phrase": "start aeri aeri-20261021-ext-r01"},
    "readiness_failed": {"first": "ports_free"},
    "key_too_soon": {"wait_s": 1.2},
    "state_not_allowed": {"state": "forward_running"},
    "profile_unknown": {"profile": "somebody-elses"},
    "prepared_changed": {"what": "job.yaml"},
    "not_prepared": {"name": "aeri-20261021-ext-r09"},
    "run_name_taken": {"name": "aeri-20261021-ext-r01"},
    "attest_incomplete": {"missing": ["scene_ready"], "unexpected": []},
}
VOLATILE = {
    "run_id": RUN_ID,
    "sent_at": "2026-10-21T10:31:05+08:00",
    "expires_at": 1792549895.0,
    "challenge": "challenge-xxxxxxxxxxxxxxxx",
    "request_id": "req-000001-test",
    "readiness_sha256": "snap-0001",
}


def _fix(value):
    if isinstance(value, dict):
        return {
            k: (
                VOLATILE[k]
                if k in VOLATILE and not isinstance(v, (dict, list))
                else _fix(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_fix(v) for v in value]
    return value


def build_answers() -> str:
    return _dump(answers())


def build_codes() -> str:
    """The closed enums and the codes of the routes, from the module: the file
    the frontend's i18n coverage test reads."""
    return _dump(
        {
            "schema": "levi.marvel0.aeri_codes.v1",
            "states": list(st.STATES),
            "actions": list(st.ACTIONS),
            "slots": list(st.SLOTS),
            "levels": list(st.LEVELS),
            "motions": list(st.MOTIONS),
            "variants": list(st.VARIANTS),
            "stop_phases": list(st.STOP_PHASES),
            "prompt_kinds": list(st.PROMPT_KINDS),
            "reasons": list(st.REASONS),
            "why_not": list(st.WHY_NOT),
            "notices": list(st.NOTICES),
            # the ticks each key (and End) is held to, in the page's order
            "key_attest": {key: list(codes) for key, codes in st.KEY_ATTEST.items()},
            "errors": {code: status for code, (status, _) in st.REFUSALS.items()},
        }
    )


def main() -> None:
    text = build()
    SAMPLE.write_text(text)
    TWIN.write_text(text)
    ANSWERS.write_text(build_answers())
    CODES.write_text(build_codes())
    names = (SAMPLE, TWIN, ANSWERS, CODES)
    print("wrote " + ", ".join(str(p.relative_to(ROOT)) for p in names))


if __name__ == "__main__":
    main()
