"""Mutation check of the fixes the two reviews of the first build asked for in the
hosted terminal D (state, start gate, Stop, site snapshot, adopt, launcher): each
fix is taken out (or its property turned round) in turn, the test file that is
meant to guard it runs, and every mutation must make at least one test fail.

    uv run --no-sync python tests/marvel0/mutate_aeri_run_fixes.py [--check] [--no-baseline] [name ...]

It is ``mutate_aeri.py`` (same copy of the sources in a temporary folder, one test
file at a time under ``nice -n 19 taskset -c 20-23``, no run while a robot process
is alive) with another list of mutations: ``--check`` says which of them do not
apply (their old text must occur exactly once)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_aeri as base

T = "tests/marvel0/"
REPLAY_STATE = T + "test_marvel0_aeri_state_replay.py"
REPLAY_RUN = T + "test_marvel0_aeri_run_replay.py"
STOP = T + "test_marvel0_aeri_run_stop.py"
GATE = T + "test_marvel0_aeri_run_gate.py"
VISIBLE = T + "test_marvel0_aeri_run_visible.py"
ADOPT = T + "test_marvel0_aeri_run_adopt.py"
STATE = T + "test_marvel0_aeri_state.py"
HOST = T + "test_marvel0_aeri_hostchild.py"
SCRIPT = T + "test_marvel0_aeri_page_stop.py"
SAMPLE = T + "test_marvel0_aeri_sample.py"

# (name, file of levi/marvel0 (or a path below it), old text that occurs exactly
# once, new text, test file)
MUTATIONS = [
    # --- the reverse loop is not a release prompt (R11 H1 = R10 5) ---------------------------
    (
        "H1: a release prompt is expected at SCENE_ASSESS again",
        "aeri_state.py",
        '            j.to_state == "VERIFY_INITIAL"\n            and j.inventory_confirmed',
        '            j.to_state in ("VERIFY_INITIAL", "SCENE_ASSESS")\n            and j.inventory_confirmed',
        REPLAY_STATE,
    ),
    (
        "H1: the release is expected by counting rounds (an abandoned round hides it)",
        "aeri_state.py",
        '            j.to_state == "VERIFY_INITIAL"\n            and j.inventory_confirmed',
        "            len(j.released) <= j.reverse_done\n            and j.inventory_confirmed",
        REPLAY_STATE,
    ),
    (
        "H1: the supervisor's clock and heartbeat make the wait unknown after 20 s",
        "aeri_state.py",
        '            j.to_state == "VERIFY_INITIAL"\n            and j.inventory_confirmed',
        '            j.to_state in ("VERIFY_INITIAL", "SCENE_ASSESS")\n            and j.inventory_confirmed',
        REPLAY_RUN,
    ),
    # --- the start gate (R11 H4 = R10 4) ------------------------------------------------------
    (
        "H4: every row that is not ok refuses (the warning of the stack row too)",
        "aeri_run.py",
        '    return level == "ok" or (level == "warn" and row.get("code") in aeri_ready.WARN_OK)',
        '    return level == "ok"',
        GATE,
    ),
    (
        "H4: a warning of any row passes",
        "aeri_run.py",
        'level == "warn" and row.get("code") in aeri_ready.WARN_OK',
        'level == "warn"',
        GATE,
    ),
    (
        "H4: unknown passes",
        "aeri_run.py",
        '    return level == "ok" or (',
        '    return level in ("ok", "unknown") or (',
        GATE,
    ),
    (
        "H4: the cards own verdict is not asked",
        "aeri_run.py",
        'if bad or snap.get("ready") is not True:',
        "if bad:",
        GATE,
    ),
    (
        "H4: the gate keeps a list of warning rows of its own",
        "aeri_run.py",
        'row.get("code") in aeri_ready.WARN_OK',
        'row.get("code") in ("stack_ready",)',
        GATE,
    ),
    # --- the wiring check: levi stop must be able to list a hosted D -----------------------------
    (
        "R10-3: a start whose D levi stop could not list goes through",
        "aeri_run.py",
        "        if not self.host.hosted_visible(self.workspace):",
        "        if False:",
        VISIBLE,
    ),
    (
        "R10-3: a D that started and is not listed keeps running",
        "aeri_run.py",
        "        if not h.hosted_visible(self.workspace, run_id=run_id):",
        "        if False:",
        VISIBLE,
    ),
    # --- a Stop from elsewhere, the record, the retry (R11 H2 = R10 6 + 7) ----------------------
    (
        "H2: a Ctrl+C D answered is no acknowledgement when this page did not send it",
        "aeri_state.py",
        '    if interrupted(f):\n        return "acked"\n    if f.stop_written == "idle":\n        return "idle"',
        '    if f.stop_written == "idle":\n        return "idle"\n    if interrupted(f):\n        return "acked"',
        STOP,
    ),
    (
        "H2: winding down ignores D's own interrupt line",
        "aeri_state.py",
        "winding = sent or acked or j.to_state",
        "winding = sent or j.to_state",
        STOP,
    ),
    (
        "H2: the record on disk is not read again",
        "aeri_run.py",
        "            return\n        self._absorb_stop(on_disk)\n",
        "            return\n        pass\n",
        STOP,
    ),
    (
        "H2: the page writes its whole record back over the script's field",
        "aeri_run.py",
        "                    on_disk = record.update_record(self._dir, **fields)",
        "                    record.write_record(self._dir, self._rec)\n                    on_disk = None",
        STOP,
    ),
    (
        "H2: the retry of a Stop goes by the pty again",
        "aeri_run.py",
        "        if not retry and master is not None and f.isig is not False and not self._found:",
        "        if master is not None and f.isig is not False and not self._found:",
        STOP,
    ),
    (
        "H2: a later Ctrl+C of the script does not use up the retry",
        "aeri_run.py",
        "            elif theirs > mine + 0.5:",
        "            elif False:",
        STOP,
    ),
    (
        "H2: the used-up retry is not kept for a restart",
        "aeri_run.py",
        "                        record.update_record(self._dir, stop_retried=True)",
        "                        pass",
        STOP,
    ),
    (
        "R10-10: a Stop D has not answered claims the arm stays where it is",
        "aeri_state.py",
        '            {} if since is None else {"since_stop_s": since},\n            may_move=unacked,',
        '            {} if since is None else {"since_stop_s": since},\n            may_move=False,',
        STOP,
    ),
    # --- the site file is not read while a run lives (R11 H3 = R10 2) ----------------------------
    (
        "H3: the state is read through the site file",
        "aeri_run.py",
        "        site = self._run_site()\n        now = self.host.now()\n        f = self._facts(site, now)",
        "        main_site = self.site_loader()\n        site = RunSite.of(main_site, self._aeri(main_site))\n        now = self.host.now()\n        f = self._facts(site, now)",
        STOP,
    ),
    (
        "H3: Stop fails when the derivation fails",
        "aeri_run.py",
        "            except Exception:  # noqa: BLE001 - nothing may take Ctrl+C away\n                return self._stop_blind(request_id)",
        "            except Exception:  # noqa: BLE001 - nothing may take Ctrl+C away\n                raise",
        STOP,
    ),
    (
        "H3: the blind Stop may be sent twice",
        "aeri_run.py",
        '        if rec.get("stop_sent_epoch") is not None:\n            raise Refuse("stop_spent")\n        path = rec.get("runner_json")',
        '        path = rec.get("runner_json")',
        STOP,
    ),
    (
        "H3: a key is pressed through the site file",
        "aeri_run.py",
        '        self._require_real()\n        directory = self._run_dir()\n        return self._transaction(\n            "key",',
        '        self._require_real()\n        directory = self._existing_folder(self.site_loader())\n        return self._transaction(\n            "key",',
        STOP,
    ),
    (
        "H3: the record keeps no snapshot of the site values",
        "aeri_run.py",
        '            "site": site.to_json(),\n',
        "",
        ADOPT,
    ),
    (
        "H3: a run is found again through the site file",
        "aeri_run.py",
        '        snap = RunSite.from_json(rec.get("site")) if rec else None\n        if snap is not None:\n            return snap\n',
        "",
        ADOPT,
    ),
    # --- the code pin (R11 M1) -------------------------------------------------------------------
    (
        "M1: any commit that has the four files passes",
        "aeri_run.py",
        "        if not st.profile_head_ok(aeri.profile, head.strip()):",
        "        if False:",
        GATE,
    ),
    (
        "M1: any head passes the profile's commit",
        "aeri_state.py",
        "        and head.lower().startswith(profile.commit.lower())",
        "        and True",
        GATE,
    ),
    # --- adopt (R11 M2 + M3, R10 9) ----------------------------------------------------------------
    (
        "M3: adopt latches before the read worked",
        "aeri_run.py",
        "            self._foreign = False\n            try:\n                done = self._adopt_read()",
        "            self._foreign = False\n            self._adopted = True\n            try:\n                done = self._adopt_read()",
        ADOPT,
    ),
    (
        "M3: a run this process started is found again after a failed first adoption",
        "aeri_run.py",
        "            if self._child is not None or self._master is not None:\n                # a run this process started",
        "            if False:\n                # a run this process started",
        ADOPT,
    ),
    (
        "M2: a dead adopted D is closed without the exit line",
        "aeri_run.py",
        "            self._finished = True\n            self._record_end(ex, site)",
        '            self._finished = True\n            self._update(phase="ended", exit=ex)',
        ADOPT,
    ),
    (
        "M2: any exit line anywhere in the events file ends the driving",
        "aeri_run.py",
        "        if events_open(text):  # the LAST start / exit line decides",
        '        if "start:" in text and "terminal D exited" not in text:',
        GATE,
    ),
    (
        "M2: a second start after an exit gets no exit line",
        "aeri_run.py",
        '            if not found or found[-1] != "terminal D exited":',
        '            if "terminal D exited" not in text:',
        ADOPT,
    ),
    (
        "R10-9: a run another live core owns is signalled and driven",
        "aeri_run.py",
        '        if self._other_live_owner(rec.get("owner"), h.self_owner()):\n            self._foreign = True',
        "        if False:\n            self._foreign = True",
        ADOPT,
    ),
    (
        "R10-9: the record names no owner",
        "aeri_run.py",
        '            "owner": h.self_owner(),',
        '            "owner": None,',
        ADOPT,
    ),
    (
        "R10-9: Stop is offered on a run another core owns",
        "aeri_state.py",
        "        alive\n        and not f.foreign\n        and (",
        "        alive\n        and (",
        ADOPT,
    ),
    (
        "R10-9: the other core never takes the run when the owner is gone",
        "aeri_run.py",
        "            if self._foreign:\n                self.adopt()  # (the owner may be gone by now: then it is ours)",
        "            if False:\n                self.adopt()",
        ADOPT,
    ),
    (
        "R10-9: a D that answered a Ctrl+C is sent another one at the adoption",
        "aeri_run.py",
        '        if rec.get("stop_sent_epoch") is None and not st.interrupted(f):',
        '        if rec.get("stop_sent_epoch") is None:',
        ADOPT,
    ),
    # --- the Enter that asks (R10 8) --------------------------------------------------------------
    (
        "R10-8: the asking Enter is a one-click key again",
        "aeri_state.py",
        "            variant = d.variant\n        if spec.wire is None:",
        '            variant = d.variant\n            if variant == "confirm_reason":\n                level, motion = "A", "none"\n        if spec.wire is None:',
        STATE,
    ),
    # --- the launcher (R11 L1) -----------------------------------------------------------------------
    (
        "L1: D's own group is not signalled",
        "hostchild.py",
        "    runner = _runner_group()\n    if runner is not None and runner != own and runner not in groups:",
        "    runner = None\n    if runner is not None and runner != own and runner not in groups:",
        HOST,
    ),
    (
        "L1: the kill after the grace forgets the groups of the hang-up",
        "hostchild.py",
        "    for group in dict.fromkeys([*_hung, *_groups()]):",
        "    for group in _groups():",
        HOST,
    ),
    (
        "L1: any process a heartbeat names is signalled (no descendant check)",
        "hostchild.py",
        "    if not _descends_from(pid, _child.pid):\n        return None\n    return pid, found",
        "    return pid, found",
        HOST,
    ),
    (
        "L1: a heartbeat of another process is trusted (no identity check)",
        "hostchild.py",
        '    if (str(ident.get("start_ticks")), str(ident.get("boot"))) != found:\n        return None',
        "    pass",
        HOST,
    ),
    (
        "L1: nobody waits for the groups once the client has ended",
        "hostchild.py",
        "        code = _wait_aeri(_child, output)\n        _await_groups()",
        "        code = _wait_aeri(_child, output)",
        HOST,
    ),
    # --- the script outside the page ----------------------------------------------------------------------
    (
        "script: a signal that fails leaves the record saying sent",
        "../../scripts/aeri_page_stop.py",
        "                mark_sent(path, previous)\n",
        "                pass\n",
        SCRIPT,
    ),
    (
        "script: a record that cannot be written keeps the signal back",
        "../../scripts/aeri_page_stop.py",
        "        noted = False\n",
        "        raise\n",
        SCRIPT,
    ),
]


if __name__ == "__main__":
    base.MUTATIONS = MUTATIONS
    sys.exit(base.main(sys.argv[1:]))
