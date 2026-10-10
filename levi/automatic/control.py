"""The command channel of a launched run (T-CL-08, design X2 §3 G4): how an
operator's ``stop``, ``resume`` and scene answer reach the runner, which may
live in another process (a systemd unit) or have been restarted.

Files in the run folder (``control/``, ``control/inbox/``,
``control/results/``; each 0700):

- ``inbox/<command_id>.json``: one command (``levi.aeri.command.v1``),
  written whole and synced, then linked into place, so a reader never sees
  half a command and an existing command is never replaced. Writing the same
  command again (same id, same content) is ``already_queued``; another
  command under a used id is refused (``command_used``). Command files stay
  after the runner crashes: the next runner (``attach``) processes the ones
  without a result, where a resume whose ``expected_seq`` has gone stale is
  refused by the orchestrator (``stale_sequence``).
- ``results/<command_id>.json``: what the runner did
  (``levi.aeri.command_result.v1``: ``ok``, ``code``, the run's state,
  ``repeated``), written once.

The runner's ``CommandPump`` thread polls the inbox every 50 ms and calls
the orchestrator's thread-safe ``stop()``/``resume()``; a command id is
idempotent there too (the same resume again is ``repeated``, never a second
transition). Every command is audited twice: an ``operator_command`` note in
the run's journal (who, when, the command id, the result code) and a line in
``$LEVI_AERI_HOME/control.jsonl``.

Everything read from a file is checked before use: names and ids match the
id pattern (no path), files are regular and not symbolic links, at most
64 KiB, strict JSON (no duplicate keys, no NaN), exactly the keys of their
kind.
"""

import contextlib
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from pathlib import Path

from . import launch
from .journal import JournalError, fsync_dir, process_identity

COMMAND_SCHEMA = "levi.aeri.command.v1"
RESULT_SCHEMA = "levi.aeri.command_result.v1"
KINDS = ("stop", "resume", "scene_answer")
POLL_S = 0.05
MAX_BYTES = 64 * 1024
CONTROL = "control"
INBOX = "inbox"
RESULTS = "results"
ID = launch.ID
FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\.json$")
NOTE_CODE = "operator_command"
# Who stops a run when its runner receives SIGTERM (systemctl --user stop).
SIGTERM_PRINCIPAL = "system:sigterm"
# States in which a SIGTERM registers no stop: the run already holds (no
# motion authority); the runner exits and leaves it as it is.
HOLD_STATES = ("WAIT_HUMAN", "FAULT_LOCKED", "COMPLETED")
# A result file that cannot be written is tried again after this, doubling
# each time, at most RESULT_MAX_TRIES times; the command is never redone.
RESULT_RETRY_S = 0.1
RESULT_RETRY_MAX_S = 5.0
RESULT_MAX_TRIES = 6
EXITING = "E_RUNNER_EXITING"
COMMON = ("schema", "command_id", "kind", "principal_id", "issued_wall_ns")
FIELDS = {
    "stop": (),
    "resume": ("expected_seq", "environment_handled", "health_rechecked"),
    "scene_answer": ("request_id", "predicates"),
}


class CommandError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _crash(point: str) -> None:
    """Crash points of a command write (tests replace it)."""


# --- folders ------------------------------------------------------------------------------------


def folders(run_dir) -> dict:
    base = Path(run_dir) / CONTROL
    return {"control": base, "inbox": base / INBOX, "results": base / RESULTS}


def ensure_folders(run_dir) -> dict:
    """The control folders, created 0700; a folder that is a symbolic link
    or not a folder is refused."""
    found = folders(run_dir)
    for path in found.values():
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
            fsync_dir(path.parent)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise CommandError("E_CONTROL", f"{path} is not a folder")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
    return found


# --- commands -----------------------------------------------------------------------------------


def _strict(data: bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise CommandError("invalid", f"duplicate key {key!r}")
            out[key] = value
        return out

    def constant(name):
        raise CommandError("invalid", f"{name} is not a number")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, CommandError):
            raise
        raise CommandError("invalid", f"not JSON: {exc}") from None


def validate(value) -> dict:
    """A command as the runner accepts it (``CommandError`` otherwise)."""
    if not isinstance(value, dict):
        raise CommandError("invalid", "a command is a JSON object")
    kind = value.get("kind")
    if kind not in KINDS:
        raise CommandError("invalid", f"kind is one of {', '.join(KINDS)}")
    expected = set(COMMON) | set(FIELDS[kind])
    if set(value) != expected:
        raise CommandError(
            "invalid", f"a {kind} command has exactly {sorted(expected)}"
        )
    if value["schema"] != COMMAND_SCHEMA:
        raise CommandError("invalid", f"schema is {COMMAND_SCHEMA}")
    for name in ("command_id", "principal_id"):
        if not isinstance(value[name], str) or not ID.fullmatch(value[name]):
            raise CommandError("invalid", f"{name} is an opaque id")
    if type(value["issued_wall_ns"]) is not int or value["issued_wall_ns"] < 0:
        raise CommandError("invalid", "issued_wall_ns is a whole number")
    if kind == "resume":
        if type(value["expected_seq"]) is not int or value["expected_seq"] < 0:
            raise CommandError("invalid", "expected_seq is a whole number")
        for name in ("environment_handled", "health_rechecked"):
            if type(value[name]) is not bool:
                raise CommandError("invalid", f"{name} is true or false")
    if kind == "scene_answer":
        if not isinstance(value["request_id"], str) or not ID.fullmatch(
            value["request_id"]
        ):
            raise CommandError("invalid", "request_id is an opaque id")
        predicates = value["predicates"]
        if (
            not isinstance(predicates, dict)
            or not 0 < len(predicates) <= 64
            or any(not ID.fullmatch(str(k)) for k in predicates)
            or any(
                v not in (True, False, None) or type(v) is int
                for v in predicates.values()
            )
        ):
            raise CommandError(
                "invalid", "predicates maps predicate names to true, false or null"
            )
    return value


def command(kind: str, command_id: str, principal_id: str, **fields) -> dict:
    """A checked command (``issued_wall_ns`` now)."""
    return validate(
        {
            "schema": COMMAND_SCHEMA,
            "command_id": command_id,
            "kind": kind,
            "principal_id": principal_id,
            "issued_wall_ns": time.time_ns(),
            **fields,
        }
    )


def new_command_id(kind: str) -> str:
    return f"{kind.replace('_', '-')}-{secrets.token_hex(8)}"


def _same(a: dict, b: dict) -> bool:
    """The same command, whenever and by whom it was issued again."""
    skip = ("issued_wall_ns",)
    return {k: v for k, v in a.items() if k not in skip} == {
        k: v for k, v in b.items() if k not in skip
    }


def read_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CommandError("invalid", f"{path.name} is not a regular file")
        data = os.read(descriptor, MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_BYTES:
        raise CommandError("invalid", f"{path.name} is larger than {MAX_BYTES} bytes")
    return data


def _link_whole(folder: Path, name: str, data: bytes) -> bool:
    """Put ``data`` at ``folder/name`` only if nothing is there: written
    whole and synced first. ``False`` when a file was there already."""
    temporary = folder / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _crash("written")
    try:
        os.link(temporary, folder / name)
    except FileExistsError:
        return False
    finally:
        _crash("linked")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    fsync_dir(folder)
    return True


def write_command(run_dir, value: dict) -> str:
    """Queue ``value`` in the run's inbox: ``queued``, or ``already_queued``
    when the same command is there; ``CommandError(command_used)`` when the
    id holds another command."""
    validate(value)
    run_dir = Path(run_dir)
    if not (run_dir / launch.LAUNCH_RECORD).is_file():
        raise CommandError("E_NOT_LAUNCHED", f"{run_dir} is not a launched run")
    found = ensure_folders(run_dir)
    name = f"{value['command_id']}.json"
    data = (json.dumps(value, sort_keys=True) + "\n").encode()
    if _link_whole(found["inbox"], name, data):
        return "queued"
    try:
        there = validate(_strict(read_file(found["inbox"] / name)))
    except (CommandError, OSError):
        there = None
    if there is not None and _same(there, value):
        return "already_queued"
    raise CommandError(
        "command_used", f"command id {value['command_id']} names another command"
    )


def read_result(run_dir, command_id: str) -> dict | None:
    if not ID.fullmatch(command_id or ""):
        raise CommandError("invalid", "not a command id")
    path = folders(run_dir)["results"] / f"{command_id}.json"
    try:
        value = _strict(read_file(path))
    except FileNotFoundError:
        return None
    return value if isinstance(value, dict) else None


def wait_result(run_dir, command_id: str, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        with contextlib.suppress(CommandError, OSError):
            found = read_result(run_dir, command_id)
            if found is not None:
                return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_S)


# --- the runner's side ---------------------------------------------------------------------------


class CommandPump(threading.Thread):
    """Polls the inbox and carries out the commands on the runner's
    orchestrator (``runner.orch``), each once. A SIGTERM the runner flagged
    becomes a stop by ``system:sigterm`` while the run is under way; while
    it holds (WAIT_HUMAN, FAULT_LOCKED) nothing is stopped and the runner
    exits, leaving the run as it is (``exited_without_stop``).

    A result file that cannot be written (disk full, read-only) is retried
    with back-off and then given up (``result_write_failed`` in the audit);
    the command itself is never carried out again by this runner. Once the
    runner's loop is over (``close``), commands still queued are answered
    ``E_RUNNER_EXITING`` and not carried out: nothing would drive the run
    after them."""

    def __init__(self, runner):
        super().__init__(name=f"aeri-control-{runner.run_id}", daemon=True)
        self.runner = runner
        self.run_dir = runner.run_dir
        self.paths = ensure_folders(self.run_dir)
        self._closed = threading.Event()
        self._tick_lock = threading.Lock()
        self._refused: set = set()
        # Command files taken by this runner (carried out or refused): never
        # taken again, whether or not their result could be written.
        self._handled: set = set()
        # name -> [result bytes, tries, next try (monotonic)]
        self._unwritten: dict = {}
        self._closing = False
        self._sigterm_result: dict | None = None
        # The latest results (bounded: a long run keeps few in memory).
        self.processed: deque = deque(maxlen=256)
        self._process = process_identity()

    # --- the loop ---------------------------------------------------------------------------------

    def run(self) -> None:
        while not self._closed.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the pump never dies silently
                self._audit_only(
                    {
                        "phase": "pump_error",
                        "detail": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
            self._closed.wait(POLL_S)

    def close(self) -> None:
        """Stop polling. Commands still queued are answered
        ``E_RUNNER_EXITING`` and not carried out."""
        self._closed.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(5)
        self._closing = True
        with contextlib.suppress(Exception):
            self.tick()

    def stop_settled(self) -> bool:
        """The SIGTERM stop has been registered (or there was none)."""
        return not self.runner.sigterm.is_set() or self._sigterm_result is not None

    def tick(self) -> None:
        with self._tick_lock:
            if self.runner.sigterm.is_set() and self._sigterm_result is None:
                self._sigterm_result = self._sigterm()
            for name in self._pending():
                self._take(name)
            self._retry_results()

    def _pending(self) -> list:
        try:
            names = os.listdir(self.paths["inbox"])
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith("."):
                continue  # a command being written
            if not FILE.fullmatch(name):
                if name not in self._refused:
                    self._refused.add(name)
                    self._audit_only(
                        {"phase": "refused", "detail": "not a command file name"}
                    )
                continue
            if name in self._handled or name in self._refused:
                continue
            if (self.paths["results"] / name).exists():
                continue
            with contextlib.suppress(OSError):
                out.append((os.lstat(self.paths["inbox"] / name).st_mtime_ns, name))
        return [name for _, name in sorted(out)]

    def _take(self, name: str) -> None:
        command_id = name[: -len(".json")]
        self._handled.add(name)
        try:
            value = validate(_strict(read_file(self.paths["inbox"] / name)))
            if value["command_id"] != command_id:
                raise CommandError("invalid", "the file name is not its command id")
        except FileNotFoundError:
            return
        except (CommandError, OSError) as exc:
            detail = exc.detail if isinstance(exc, CommandError) else str(exc)
            self._refused.add(name)
            self._finish(
                {"command_id": command_id, "kind": None, "principal_id": None},
                {"ok": False, "code": "invalid", "detail": detail[:300]},
            )
            return
        if self._closing:
            self._finish(value, self._exiting())
            return
        self._finish(value, self._execute(value))

    def _exiting(self) -> dict:
        orch = self.runner.orch
        return {
            "ok": False,
            "code": EXITING,
            "detail": "the runner was exiting: not carried out; send it again "
            "(a new command id) once a runner serves the run (attach)",
            "state": orch.state if orch is not None else None,
        }

    # --- doing a command ----------------------------------------------------------------------------

    def _execute(self, value: dict) -> dict:
        orch = self.runner.orch
        kind = value["kind"]
        try:
            if kind == "stop":
                found = orch.stop(value["command_id"])
            elif kind == "resume":
                found = orch.resume(
                    value["command_id"],
                    expected_seq=value["expected_seq"],
                    environment_handled=value["environment_handled"],
                    health_rechecked=value["health_rechecked"],
                )
            else:
                return self._answer(value)
        except Exception as exc:  # noqa: BLE001 - the orchestrator locked the run
            return {
                "ok": False,
                "code": "error",
                "detail": f"{type(exc).__name__}: {exc}"[:300],
                "state": orch.state,
            }
        finally:
            self.runner.wake.set()
        return {
            "ok": found.ok,
            "code": found.code,
            "state": found.state,
            "repeated": found.repeated,
            "sequence_no": found.sequence_no,
        }

    def _answer(self, value: dict) -> dict:
        """A person's answer to an open scene question (operator_attested):
        given to the scene check's transport as a page would; whether it is
        taken is the check's decision (its nonce and frames)."""
        orch = self.runner.orch
        transport = getattr(getattr(orch, "scene", None), "transport", None)
        answer = getattr(transport, "answer", None)
        if not callable(answer):
            return {
                "ok": False,
                "code": "not_supported",
                "detail": "this run's scene check asks no person",
                "state": orch.state,
            }
        questions = getattr(transport, "questions", None)
        if isinstance(questions, dict) and value["request_id"] not in questions:
            return {
                "ok": False,
                "code": "unsolicited",
                "detail": "no open question has this request id",
                "state": orch.state,
            }
        answer(value["request_id"], dict(value["predicates"]))
        return {"ok": True, "code": "delivered", "state": orch.state}

    def _sigterm(self) -> dict:
        """SIGTERM: a controlled stop while the run is under way; nothing
        while it already holds (review CL3 B3: a stopped unit, a Ctrl+C or
        a logout must not end a run that waits for a person; only a typed
        ``stop`` does)."""
        value = command(
            "stop",
            f"sigterm-{os.getpid()}-{self.runner.started}",
            SIGTERM_PRINCIPAL,
        )
        orch = self.runner.orch
        state = orch.state if orch is not None else None
        if self._closing or state in HOLD_STATES:
            result = {
                "ok": True,
                "code": "exited_without_stop",
                "detail": "the run holds: no stop registered, the runner exits "
                "and leaves the run as it is (attach locks it)",
                "state": state,
            }
        else:
            result = self._execute(value)
        self._finish(value, result)
        return result

    # --- results and audit ------------------------------------------------------------------------

    def _finish(self, value: dict, result: dict) -> None:
        record = {
            "schema": RESULT_SCHEMA,
            "command_id": value["command_id"],
            "kind": value["kind"],
            "principal_id": value["principal_id"],
            "processed_wall_ns": time.time_ns(),
            "repeated": False,
            "sequence_no": None,
            "state": None,
            "detail": "",
            **result,
        }
        data = (json.dumps(record, sort_keys=True) + "\n").encode()
        name = f"{value['command_id']}.json"
        try:
            _link_whole(self.paths["results"], name, data)
        except OSError as exc:
            self._unwritten[name] = [
                data,
                1,
                time.monotonic() + RESULT_RETRY_S,
                str(exc),
            ]
        self.processed.append(record)
        self._note(record)
        self._audit_only(
            {
                "phase": "result",
                "command_id": record["command_id"],
                "kind": record["kind"],
                "principal_id": record["principal_id"],
                "code": record["code"],
                "state": record["state"],
            }
        )

    def _retry_results(self) -> None:
        """Write the results that could not be written, with back-off; give
        up after ``RESULT_MAX_TRIES`` (audited, noted once)."""
        now = time.monotonic()
        for name, entry in list(self._unwritten.items()):
            data, tries, due, error = entry
            if now < due and not self._closing:
                continue
            try:
                _link_whole(self.paths["results"], name, data)
            except OSError as exc:
                tries += 1
                if tries < RESULT_MAX_TRIES and not self._closing:
                    delay = min(RESULT_RETRY_MAX_S, RESULT_RETRY_S * 2 ** (tries - 1))
                    self._unwritten[name] = [data, tries, now + delay, str(exc)]
                    continue
                error = str(exc)
            else:
                del self._unwritten[name]
                continue
            del self._unwritten[name]
            command_id = name[: -len(".json")]
            self._audit_only(
                {
                    "phase": "result_write_failed",
                    "command_id": command_id,
                    "code": "failed",
                    "detail": f"{tries} tries: {error}"[:300],
                }
            )
            orch = self.runner.orch
            if orch is not None:
                with contextlib.suppress(JournalError):
                    orch.journal.note(
                        "command_result_lost",
                        f"{command_id}: its result could not be written ({error})"[
                            :300
                        ],
                        authority=self._authority(None),
                    )

    def _authority(self, principal) -> dict:
        return {
            "principal_kind": "operator" if principal else "orchestrator",
            "principal_id": principal
            if principal and ID.fullmatch(principal)
            else self.runner.orch.config.principal_id,
            "session_id": self.runner.orch.config.session_id,
            "process": self._process,
            "command_id": None,
        }

    def _note(self, record: dict) -> None:
        orch = self.runner.orch
        if orch is None:
            return
        detail = (
            f"{record['kind']} {record['command_id']} by {record['principal_id']}: "
            f"{record['code']}"
            + (" (repeated)" if record.get("repeated") else "")
            + f" at {record['processed_wall_ns']}"
        )
        principal = record["principal_id"]
        authority = {
            "principal_kind": "operator",
            "principal_id": principal
            if principal and ID.fullmatch(principal)
            else "unknown",
            "session_id": orch.config.session_id,
            "process": self._process,
            # No command id: the journal indexes commands by the lines of
            # their transitions; an audit line must not look like one.
            "command_id": None,
        }
        with contextlib.suppress(JournalError):
            orch.journal.note(NOTE_CODE, detail[:300], authority=authority)

    def _audit_only(self, event: dict) -> None:
        launch.audit(
            {
                "at_wall_ns": time.time_ns(),
                "run_id": self.runner.run_id,
                **event,
            },
            self.runner.path,
        )
