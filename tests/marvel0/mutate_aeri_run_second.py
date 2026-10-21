"""Mutation check of the fixes the second reviews (safety and mechanics) asked for in
the hosted terminal D: each fix is taken out (or its property turned round) in turn,
the test file that is meant to guard it runs, and every mutation must make at least
one test fail.

    MUTATE_CPUS=16-19 uv run --no-sync python tests/marvel0/mutate_aeri_run_second.py [--check] [--no-baseline] [name ...]

It is ``mutate_aeri.py`` (same copy of the sources in a temporary folder, one test
file at a time under ``nice -n 19 taskset``, no run while a robot process is alive)
with another list of mutations: ``--check`` says which of them do not apply (their
old text must occur exactly once). The names start with the finding: ``N1`` ..
``N13`` of the safety review, ``M-N3``, ``M-N4`` of the mechanics review."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_aeri as base

T = "tests/marvel0/"
STATE = T + "test_marvel0_aeri_state.py"
STOP = T + "test_marvel0_aeri_run_stop.py"
ADOPT = T + "test_marvel0_aeri_run_adopt.py"
RUN = T + "test_marvel0_aeri_run.py"
ROUTE = T + "test_marvel0_aeri_run_route.py"
SCRIPT = T + "test_marvel0_aeri_page_stop.py"
ACTIVITY = T + "test_marvel0_aeri_activity.py"
PROBES = "tests/test_setup_probes.py"
READY = T + "test_marvel0_aeri_ready.py"
GATE = T + "test_marvel0_aeri_run_gate.py"

# (name, file of levi/marvel0 (or a path below it), old text that occurs exactly
# once, new text, test file)
MUTATIONS = [
    # --- N1: the retry in every wrap-up, and an End that can be reached ------------------------
    (
        "N1: the retry is only offered while stopping",
        "aeri_state.py",
        'RETRY_STATES = ("stopping", "stalled", "orphaned")',
        'RETRY_STATES = ("stopping",)',
        STATE,
    ),
    (
        "N1: the retry is not offered to an adopted (orphaned) D",
        "aeri_state.py",
        'RETRY_STATES = ("stopping", "stalled", "orphaned")',
        'RETRY_STATES = ("stopping", "stalled")',
        STOP,
    ),
    (
        "N1: the retry is not offered to a stalled D",
        "aeri_state.py",
        'RETRY_STATES = ("stopping", "stalled", "orphaned")',
        'RETRY_STATES = ("stopping", "orphaned")',
        STOP,
    ),
    (
        "N1: End after the retry is not offered (only a stall opens it)",
        "aeri_state.py",
        'phase == "retried"\n                and f.stop_last_age_s is not None',
        'phase == "never"\n                and f.stop_last_age_s is not None',
        STOP,
    ),
    (
        "N1: End opens after the retry although D answered",
        "aeri_state.py",
        'phase == "retried"\n                and f.stop_last_age_s is not None',
        'phase in ("retried", "acked")\n                and f.stop_last_age_s is not None',
        STOP,
    ),
    (
        "N1: End opens before the retry was used",
        "aeri_state.py",
        'phase == "retried"\n                and f.stop_last_age_s is not None',
        'phase in ("retried", "unconfirmed")\n                and f.stop_last_age_s is not None',
        STOP,
    ),
    (
        "N1: End opens at once after the retry (no minute)",
        "aeri_state.py",
        "and f.stop_last_age_s >= END_AFTER_RETRY_S",
        "and f.stop_last_age_s >= 0",
        STATE,
    ),
    (
        "N1: End opens for a run another core owns",
        "aeri_state.py",
        "        alive\n        and not f.foreign\n        and not f.blind\n",
        "        alive\n        and not f.blind\n",
        STATE,
    ),
    (
        "N1: the retry time is not kept (End counts from the first Ctrl+C)",
        "aeri_run.py",
        '{"stop_retried": True, "stop_retry_epoch": f.now}',
        '{"stop_retried": True}',
        STOP,
    ),
    (
        "N1: the age of the last Ctrl+C is the first one's",
        "aeri_run.py",
        "    last = max((t for t in (sent, retry) if t is not None), default=None)",
        "    last = sent",
        STOP,
    ),
    # --- N7 = M-N1: one failed read of /proc ---------------------------------------------------
    (
        "N7: our own launcher is judged by /proc as well as by poll()",
        "aeri_run.py",
        '            return True, None\n        if self._foreign and rec.get("phase") == "intent":',
        '            return self.host.proc_state(child.pid) is not None, None\n        if self._foreign and rec.get("phase") == "intent":',
        RUN,
    ),
    (
        "N7: an adopted launcher is gone after one missed read",
        "aeri_run.py",
        "        for attempt in range(2):\n            if attempt:\n                h.sleep(MISS_RECHECK_S)",
        "        for attempt in range(1):\n            if attempt:\n                h.sleep(MISS_RECHECK_S)",
        ADOPT,
    ),
    (
        "N7: the second read comes at once, with no pause",
        "aeri_run.py",
        "                h.sleep(MISS_RECHECK_S)",
        "                pass",
        ADOPT,
    ),
    (
        "N7: a pid that answers as another process is looked at again",
        "aeri_run.py",
        "            if who is not None and who != want:\n                return False\n",
        "",
        ADOPT,
    ),
    # --- N8: a record nobody can read is a D that may be running -----------------------------------
    (
        "N8: active() says no run for a record that cannot be read",
        "aeri_run.py",
        "                if self._unreadable:\n                    # The record exists and cannot be read",
        "                if False:\n                    # The record exists and cannot be read",
        ADOPT,
    ),
    (
        "N8: the adoption does not remember that the record could not be read",
        "aeri_run.py",
        "            self._unreadable = True\n            raise",
        "            raise",
        ADOPT,
    ),
    (
        "N8: the flag stays for ever (a repaired record is not believed)",
        "aeri_run.py",
        "        self._unreadable = False\n        self._dir = directory",
        "        self._dir = directory",
        ADOPT,
    ),
    (
        "N8: a start goes ahead beside a record nobody can read",
        "aeri_run.py",
        '            if self._unreadable:\n                raise Refuse("d_active", record="unreadable")',
        "            if False:\n                raise Refuse('d_active', record='unreadable')",
        ADOPT,
    ),
    (
        "N8: the document does not say why a start is refused",
        "aeri_run.py",
        '        if self._unreadable:\n            return st.why("d_active", record="unreadable")',
        "        if False:\n            return None",
        ADOPT,
    ),
    (
        "N8 script: --scope works beside a record that can be read",
        "../../scripts/aeri_page_stop.py",
        '    if args.scope:\n        print(\n            "--scope is for a record that cannot be read;',
        '    if False:\n        print(\n            "--scope is for a record that cannot be read;',
        SCRIPT,
    ),
    (
        "N8 script: a launcher that does not lead its group gets the group signal",
        "../../scripts/aeri_page_stop.py",
        "if is_launcher(words_of(pid)) and os.getpgid(pid) == pid:",
        "if is_launcher(words_of(pid)):",
        SCRIPT,
    ),
    (
        "N8 script: uv (same words as D) counts as terminal D",
        "../../scripts/aeri_page_stop.py",
        '    if not words or not os.path.basename(words[0]).startswith("python"):',
        "    if not words:",
        SCRIPT,
    ),
    (
        "N8 script: two scopes are not refused",
        "../../scripts/aeri_page_stop.py",
        "    if len(scopes) > 1:",
        "    if len(scopes) > 99:",
        SCRIPT,
    ),
    (
        "N8 script: a scope of another user's tree counts",
        "../../scripts/aeri_page_stop.py",
        '    base = Path(root) / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"',
        '    base = Path(root) / "user.slice"',
        SCRIPT,
    ),
    # --- N12: a live run that is not followed is not another core's ------------------------------------
    (
        "N12: an unfollowed run is shown as another core's",
        "aeri_run.py",
        "f.alive, f.blind = True, True",
        "f.alive, f.foreign = True, True",
        STOP,
    ),
    (
        "N12: the unfollowed run forgets that a Stop was sent",
        "aeri_run.py",
        "                    for name, value in stop_facts(rec, now).items():\n                        setattr(f, name, value)",
        "                    pass",
        STOP,
    ),
    # --- N13: the record says sent before the signal, under the lock ---------------------------------
    (
        "N13: the page signals first and writes after",
        "aeri_run.py",
        "            self._update(_disk=disk, **fields)\n            try:\n                via = self._send_stop(f, retry=retry)\n            except BaseException:\n                self._update(_disk=disk, **before)\n                raise",
        "            try:\n                via = self._send_stop(f, retry=retry)\n            except BaseException:\n                raise\n            self._update(_disk=disk, **fields)",
        STOP,
    ),
    (
        "N13: a signal that cannot be sent leaves the record saying sent",
        "aeri_run.py",
        "                self._update(_disk=disk, **before)\n                raise",
        "                raise",
        STOP,
    ),
    (
        "N13: the record's lock is not really taken",
        "aeri_run.py",
        "                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n                        held = True",
        "                        held = True",
        STOP,
    ),
    (
        "N13: what the script wrote is not read again under the lock",
        "aeri_run.py",
        "            if held:\n                self._merge_disk(held=True)",
        "            if False:\n                self._merge_disk(held=True)",
        STOP,
    ),
    (
        "N13: a decision the script overtook is carried out anyway",
        "aeri_run.py",
        '            if (seen.get("stop_sent_epoch") is None) != (\n                rec.get("stop_sent_epoch") is None\n            ) or bool(seen.get("stop_retried")) != bool(rec.get("stop_retried")):\n                raise Refuse("stop_spent")',
        "            if False:\n                raise Refuse('stop_spent')",
        STOP,
    ),
    (
        "N13: a Ctrl+C waits long for the record's lock",
        "aeri_run.py",
        "                deadline = self.host.monotonic() + RECORD_LOCK_WAIT_S",
        "                deadline = self.host.monotonic() + 8.0",
        STOP,
    ),
    (
        "N13 script: the check for an earlier Stop is not repeated under the lock",
        "../../scripts/aeri_page_stop.py",
        '        if refuse_if_sent and data.get("stop_sent_epoch"):',
        "        if False:",
        SCRIPT,
    ),
    (
        "N13 script: it waits long for the record's lock",
        "../../scripts/aeri_page_stop.py",
        "        deadline = time.monotonic() + LOCK_WAIT_S",
        "        deadline = time.monotonic() + 30.0",
        SCRIPT,
    ),
    # --- N3: a key does not outlive the switch -------------------------------------------------------
    (
        "N3: the switch is not looked at when the keys are offered",
        "aeri_state.py",
        "        elif not c.enabled:\n            # The switch of the control file",
        "        elif False:\n            # The switch of the control file",
        STATE,
    ),
    (
        "N3: a press goes through although the switch is off",
        "aeri_run.py",
        "                enabled=bool(self.host.control_enabled()),",
        "                enabled=True,",
        RUN,
    ),
    # --- N6: the ticks are held to on the server -------------------------------------------------------
    (
        "N6: a key is taken without its ticks",
        "aeri_run.py",
        '            self._check_attest(action, body.get("attest"))\n            spec = st.KEYS[action]',
        "            spec = st.KEYS[action]",
        RUN,
    ),
    (
        "N6: End is taken without its ticks",
        "aeri_run.py",
        '            self._check_attest("end", attest)',
        "            pass",
        RUN,
    ),
    (
        "N6: more ticks than the key needs are taken",
        "aeri_run.py",
        "        if got is None or sorted(got) != sorted(need):",
        "        if got is None or not set(need) <= set(got):",
        RUN,
    ),
    (
        "N6: a tick sent twice is taken",
        "aeri_run.py",
        "        if got is None or sorted(got) != sorted(need):",
        "        if got is None or set(got) != set(need):",
        RUN,
    ),
    (
        "N6: the release of a round needs no tick",
        "aeri_state.py",
        '    "release_round": ("scene_ready",),',
        '    "release_round": (),',
        STATE,
    ),
    (
        "N6: the route takes any word as a tick",
        "aeri_run_api.py",
        '    confirm: str = ""\n    # The ticks of the key\'s window, exactly those of ``aeri_state.KEY_ATTEST[action]``\n    # (none for a key not in it): the server holds the key to them.\n    attest: list[Tick] = []',
        '    confirm: str = ""\n    attest: list[str] = []',
        ROUTE,
    ),
    # --- M-N3: a core that does not own the run does not wait for it ---------------------------------
    (
        "M-N3: the shutdown of a core that does not own the run waits the whole time",
        "aeri_activity.py",
        '    if sent == "driven_elsewhere":',
        "    if False:",
        ACTIVITY,
    ),
    # --- M-N4: the two gaps of the adoption -------------------------------------------------------------
    (
        "M-N4a: a launcher with no identity on record counts as alive when its pid is gone",
        "aeri_run.py",
        '            and self._pid_alive(pid, leader.get("identity"))',
        '            and self.host.identity(pid) == leader.get("identity")',
        ADOPT,
    ),
    (
        "M-N4b: the owner is looked at after the launcher is judged gone",
        "aeri_run.py",
        '        if self._other_live_owner(rec.get("owner"), h.self_owner()):\n            size = self._screen_size(site, name)',
        "        if False:\n            size = self._screen_size(site, name)",
        ADOPT,
    ),
    (
        "M-N4b: a launch under way in another core is shown as ended",
        "aeri_run.py",
        '        if self._foreign and rec.get("phase") == "intent":\n            return True, None  # the core that owns it is still making the launch',
        "",
        ADOPT,
    ),
    # --- N4 = M-N5: the product unit must give the shutdown its time (a warning row) --------------
    (
        "N4: a unit that stops after 15 s is called fine",
        "aeri_ready.py",
        '    if seconds >= need:\n        return row("unit_timeout", True, seconds=shown, **params)',
        '    if True:\n        return row("unit_timeout", True, seconds=shown, **params)',
        READY,
    ),
    (
        "N4: a unit that cannot be asked is called fine",
        "aeri_ready.py",
        '    if not isinstance(unit_stop, dict):\n        return _warn("unit_timeout", CHECK_UNKNOWN, seconds=None, **params)',
        '    if not isinstance(unit_stop, dict):\n        return row("unit_timeout", True, seconds=None, **params)',
        READY,
    ),
    (
        "N4: a unit that cannot be asked refuses the start",
        "aeri_ready.py",
        '    if not isinstance(unit_stop, dict):\n        return _warn("unit_timeout", CHECK_UNKNOWN, seconds=None, **params)',
        '    if not isinstance(unit_stop, dict):\n        return row("unit_timeout", None, seconds=None, **params)',
        READY,
    ),
    (
        "N4: the warning is not one a start lets through",
        "aeri_ready.py",
        'WARN_ROWS = ("stack_ready", "unit_timeout")',
        'WARN_ROWS = ("stack_ready",)',
        GATE,
    ),
    (
        "N4: the unit is asked at every read (no cache)",
        "aeri_ready.py",
        "    if seen is not None and now - seen[0] < UNIT_CACHE_S:",
        "    if False:",
        READY,
    ),
    (
        "N4: the unit is asked with a verb that changes something",
        "aeri_ready.py",
        '                "show",\n                unit,\n                "--property=LoadState",',
        '                "restart",\n                unit,\n                "--property=LoadState",',
        READY,
    ),
    # --- N9 = M-N2: the setup banner ------------------------------------------------------------------------
    (
        "N9: a hosted session is a product job in the guard banner",
        "../setup/probes.py",
        'running = [j for j in ((jobs or {}).get("jobs") or []) if not j.get("hosted")]',
        'running = [j for j in ((jobs or {}).get("jobs") or [])]',
        PROBES,
    ),
]


if __name__ == "__main__":
    base.MUTATIONS = MUTATIONS
    # (the readiness tests read the page's poll time from its source)
    base.COPIED = (*base.COPIED, "src/components/marvel0/autonomy-logic.ts")
    sys.exit(base.main(sys.argv[1:]))
