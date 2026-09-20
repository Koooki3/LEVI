"""Local ACP session adapter. No transcript scraping or implicit model fallback."""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .grants import create
from .pilot_contracts import GrantRequest
from .runtime import new_id

_ACTIVE = {}
_LOCK = threading.Lock()
VERSIONS = {"codex": "1.12.0", "claude": "0.79.0"}


def command(runtime):
    from levi.paths import PROJECT

    name = "codex-acp" if runtime == "codex" else "claude-agent-acp"
    return PROJECT / "integrations/pilot/node_modules/.bin" / name


def profiles():
    return [
        {
            "id": key,
            "version": version,
            "installed": command(key).exists(),
            "authentication": "unknown",
            "login_owner": "official-local-client",
            "capabilities": "negotiated-on-connect",
            "login_command": "codex login" if key == "codex" else "claude auth login",
            "logout_command": "codex logout"
            if key == "codex"
            else "claude auth logout",
        }
        for key, version in VERSIONS.items()
    ]


class ACP:
    """Bounded JSON-RPC reader; notifications and requests can interleave."""

    def __init__(self, argv, cwd, env, notify, permission, timeout=90):
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.notify = notify
        self.permission = permission
        self.timeout = timeout
        self.pending = {}
        self.sequence = 0
        self.lock = threading.Lock()
        self.writer = threading.Lock()
        self.closed = threading.Event()
        self.closing = threading.Lock()
        threading.Thread(target=self.read, daemon=True).start()

    def send(self, value):
        with self.writer:
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **value}) + "\n")
            self.process.stdin.flush()

    def read(self):
        try:
            while True:
                line = self.process.stdout.readline(2 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 2 * 1024 * 1024:
                    raise ValueError("ACP event exceeds size limit")
                event = json.loads(line)
                if "method" in event:
                    if "id" in event:
                        if event["method"] == "session/request_permission":
                            result = self.permission(event.get("params", {}))
                            self.send({"id": event["id"], "result": result})
                        else:
                            self.send(
                                {
                                    "id": event["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "LEVI does not expose terminal/filesystem methods",
                                    },
                                }
                            )
                    else:
                        self.notify(event["method"], event.get("params", {}))
                elif event.get("id") in self.pending:
                    self.pending[event["id"]].put(event)
        except (ValueError, OSError):
            pass
        finally:
            self.closed.set()
            for pending in list(self.pending.values()):
                pending.put({"error": {"message": "ACP connection closed"}})

    def call(self, method, params, timeout=None):
        with self.lock:
            self.sequence += 1
            id = self.sequence
            receiver = self.pending[id] = queue.Queue()
        try:
            self.send({"id": id, "method": method, "params": params})
            event = receiver.get(timeout=timeout or self.timeout)
            if "error" in event:
                raise ValueError(
                    "ACP request failed: "
                    + str(event["error"].get("message", "protocol error"))[:400]
                )
            return event.get("result", {})
        finally:
            self.pending.pop(id, None)

    def close(self):
        with self.closing:
            self._close()

    def _close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


class Session:
    def __init__(self, wb, value, token):
        self.wb, self.value, self.token = wb, value, token
        self.messages = queue.Queue()
        self.stop = threading.Event()
        from .runtime_adapters import RuntimeTransport

        self.acp: RuntimeTransport | None = None
        self.remote_id = None
        self.pending_permissions = {}

    def update(self, **values):
        self.wb.store.mutate(
            "pilot_sessions", self.value["id"], lambda s: s.update(**values)
        )
        self.wb.store.event(
            self.value["run_id"], "pilot.status", session_id=self.value["id"], **values
        )

    def notify(self, method, params):
        if method != "session/update":
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate", "unknown")
        # Public activity only; never retain hidden/model thought blocks.
        if kind in {"agent_thought_chunk"}:
            return
        allowed = {
            "agent_message_chunk",
            "tool_call",
            "tool_call_update",
            "plan",
            "usage_update",
            "current_mode_update",
            "session_info_update",
            "config_option_update",
        }
        if kind not in allowed:
            return
        raw = json.dumps(update, ensure_ascii=False)
        # Redact credentials before retaining publicly emitted runtime events.
        for value in [self.token] + [
            v
            for k, v in os.environ.items()
            if any(w in k.upper() for w in ("TOKEN", "SECRET", "API_KEY"))
            and len(v) > 8
        ]:
            raw = raw.replace(value, "[redacted]")
        run = self.wb.store.get("runs", self.value["run_id"])
        artifact = self.wb.store.run_dir(run["id"]) / (
            "runtime-events-" + self.value["id"] + ".jsonl"
        )
        size = len(raw.encode()) + 1
        if (
            run.get("pilot_event_bytes", 0) + size
            > run["context"]["budget"]["max_artifact_bytes"]
        ):
            self.stop.set()
            self.wb.store.mutate(
                "grants", self.value["grant_id"], lambda g: g.update(enabled=False)
            )
            raise ValueError("Runtime event artifact budget exhausted")
        self.wb.store.mutate(
            "runs",
            run["id"],
            lambda r: r.update(pilot_event_bytes=r.get("pilot_event_bytes", 0) + size),
        )
        with artifact.open("a") as handle:
            handle.write(raw + "\n")
        self.wb.store.event(
            self.value["run_id"],
            "pilot.activity",
            session_id=self.value["id"],
            source="runtime",
            activity=json.loads(raw[:64000])
            if len(raw) <= 64000
            else {
                "sessionUpdate": kind,
                "artifact": artifact.name,
                "summary": "Large runtime event retained in artifact",
            },
        )

    def permission(self, params):
        call = params.get("toolCall", {})
        # ACP terminal/file access is not part of LEVI's business capabilities.
        if call.get("kind") in {
            "execute",
            "edit",
            "delete",
            "move",
            "read",
            "search",
            "fetch",
        }:
            return {"outcome": {"outcome": "cancelled"}}
        id = new_id()
        value = {
            "id": id,
            "session_id": self.value["id"],
            "run_id": self.value["run_id"],
            "tool": str(call.get("title", "Tool"))[:300],
            "options": [
                {"optionId": x["optionId"], "name": x["name"], "kind": x["kind"]}
                for x in params.get("options", [])
                if x["kind"] in {"allow_once", "reject_once"}
            ],
            "status": "pending",
        }
        self.wb.store.put("pilot_permissions", id, value)
        self.wb.store.event(self.value["run_id"], "pilot.permission", permission=value)
        deadline = time.monotonic() + 120
        while not self.stop.wait(0.2) and time.monotonic() < deadline:
            answer = self.wb.store.get("pilot_permissions", id)
            if answer["status"] == "answered":
                return {
                    "outcome": {"outcome": "selected", "optionId": answer["option_id"]}
                }
        self.wb.store.mutate(
            "pilot_permissions", id, lambda p: p.update(status="expired")
        )
        return {"outcome": {"outcome": "cancelled"}}

    def run(self):
        started = time.monotonic()
        try:
            from .planning import require

            run = self.wb.store.get("runs", self.value["run_id"])
            folder = self.wb.store.run_dir(run["id"]) / "session" / self.value["id"]
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            env = dict(os.environ)
            for name in ["LEVI_UI_TOKEN", "LEVI_AGENT_TOKEN", "LEVI_AGENT_GRANT_FILE"]:
                env.pop(name, None)
            env["LEVI_PILOT_CHILD"] = "1"
            env["INITIAL_AGENT_MODE"] = "read-only"
            if self.value["runtime"] == "codex":
                env["CODEX_CONFIG"] = json.dumps(
                    {
                        "features.shell_tool": False,
                        "features.unified_exec": False,
                        "features.multi_agent": False,
                        "web_search": "disabled",
                        "approval_policy": "untrusted",
                        "sandbox_mode": "read-only",
                    }
                )
            self.acp = ACP(
                [str(command(self.value["runtime"]))],
                folder,
                env,
                self.notify,
                self.permission,
            )
            capabilities = self.acp.call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "LEVI", "version": "0.3.0"},
                },
            )
            if run["context"]["cameras"] and not capabilities.get(
                "agentCapabilities", {}
            ).get("promptCapabilities", {}).get("image"):
                raise ValueError(
                    "Runtime does not advertise image input; visual annotation is blocked"
                )
            from levi.paths import ROOT

            server = {
                "name": "levi",
                "command": sys.executable,
                "args": ["-m", "levi.agent.control", "mcp"],
                "env": [
                    {"name": "LEVI_WORKSPACE", "value": str(ROOT)},
                    {"name": "LEVI_AGENT_TOKEN", "value": self.token},
                    {"name": "LEVI_PILOT_CHILD", "value": "1"},
                ],
            }
            options = {"cwd": str(folder), "mcpServers": [server]}
            if self.value["runtime"] == "claude":
                options["_meta"] = {
                    "claudeCode": {
                        "options": {
                            "tools": [],
                            "settingSources": [],
                            "allowDangerouslySkipPermissions": False,
                            "extraArgs": {"strict-mcp-config": ""},
                            "maxTurns": self.value["max_turns"],
                        }
                    }
                }
            result = self.acp.call("session/new", options)
            self.remote_id = result["sessionId"]
            self.update(
                connection="connected",
                state="idle",
                remote_id=self.remote_id,
                capabilities=capabilities,
                authentication="ready",
            )
            deadline = time.monotonic() + self.value["max_seconds"]
            while not self.stop.is_set():
                if time.monotonic() >= deadline:
                    raise ValueError("Pilot duration budget exhausted")
                try:
                    text = self.messages.get(timeout=0.25)
                except queue.Empty:
                    continue
                run = self.wb.store.get("runs", self.value["run_id"])
                require(self.wb, run)
                current = self.wb.store.get("pilot_sessions", self.value["id"])
                if (
                    current["turns"] >= current["max_turns"]
                    or run.get("pilot_turns", 0)
                    >= run["context"]["budget"]["max_calls"]
                ):
                    raise ValueError("Pilot turn budget exhausted")
                self.wb.store.mutate(
                    "runs",
                    run["id"],
                    lambda r: r.update(pilot_turns=r.get("pilot_turns", 0) + 1),
                )
                self.update(state="running", turns=current["turns"] + 1)
                # Only load overview and the task-specific skill, never every workflow.
                kind = run["context"]["workflow"]["kind"]
                skills = [
                    "levi-overview",
                    {
                        "review": "dataset-review",
                        "temporal": "temporal-annotation",
                        "objects": "object-annotation",
                    }[kind],
                ]
                guidance = "\n".join(
                    (Path(__file__).parent / "skills" / s / "SKILL.md").read_text()
                    for s in skills
                )
                prompt = f"{guidance}\nLEVI run: {run['id']}. Use only supplied LEVI MCP tools. Never approve your own work. Read the approved plan and work only within it. Stop for pilot review, human corrections or commit approval. Tools enforce scope.\nUser request: {text}"
                result = self.acp.call(
                    "session/prompt",
                    {
                        "sessionId": self.remote_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                    timeout=max(1, deadline - time.monotonic()),
                )
                self.update(
                    state="idle",
                    stop_reason=result.get("stopReason"),
                    usage_status="unknown-unless-reported",
                )
        except (
            ValueError,
            OSError,
            KeyError,
            RuntimeError,
            queue.Empty,
            PermissionError,
        ) as exc:
            self.update(
                state="blocked" if not self.stop.is_set() else "paused",
                reason=str(exc).replace(self.token, "[redacted]")[:500],
            )
        finally:
            self.wb.store.mutate(
                "runs",
                self.value["run_id"],
                lambda r: r.update(
                    pilot_seconds=r.get("pilot_seconds", 0)
                    + time.monotonic()
                    - started,
                    pilot_reserved_seconds=max(
                        0,
                        r.get("pilot_reserved_seconds", 0) - self.value["max_seconds"],
                    ),
                ),
            )
            if self.acp:
                self.acp.close()
            self.update(connection="disconnected")
            self.wb.store.mutate(
                "grants", self.value["grant_id"], lambda g: g.update(enabled=False)
            )
            with _LOCK:
                _ACTIVE.pop(self.value["id"], None)


def start(wb, spec):
    from .planning import require

    run = wb.store.get("runs", spec.run_id)
    require(wb, run)
    if run["status"] in {"cancelled", "succeeded"}:
        raise ValueError("Terminal task cannot start another Pilot")
    if run["context"]["provider"] != "external":
        raise ValueError(
            "Pilot requires an external-runtime plan; API runs use the API channel"
        )
    if run["context"].get("pilot_runtime") != spec.runtime:
        raise ValueError(
            "Plan must explicitly bind pilot_runtime to codex or claude before approval"
        )
    if run.get("pilot_turns", 0) >= run["context"]["budget"]["max_calls"]:
        raise ValueError("Pilot turn budget exhausted; revise and approve the plan")
    remaining = (
        run["context"]["budget"]["max_seconds"]
        - run.get("pilot_seconds", 0)
        - run.get("pilot_reserved_seconds", 0)
    )
    if remaining <= 0:
        raise ValueError("Pilot duration budget exhausted; revise and approve the plan")
    spec = spec.model_copy(
        update={
            "max_seconds": min(spec.max_seconds, remaining),
            "max_turns": min(
                spec.max_turns,
                run["context"]["budget"]["max_calls"] - run.get("pilot_turns", 0),
            ),
        }
    )
    if not command(spec.runtime).exists():
        raise ValueError(
            "Install optional runtimes: cd integrations/pilot && bun install --frozen-lockfile"
        )
    if not shutil.which("node"):
        raise ValueError("Pilot adapters require Node.js 22 or newer")
    node_version = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, timeout=5, check=True
    ).stdout.strip()
    if int(node_version.lstrip("v").split(".")[0]) < 22:
        raise ValueError("Pilot adapters require Node.js 22 or newer")
    with _LOCK:
        if any(s.value["run_id"] == run["id"] for s in _ACTIVE.values()):
            raise ValueError("This run already has a managed Pilot session")
        grant = create(
            wb.store,
            GrantRequest(
                client=spec.runtime,
                datasets=[run["context"]["repo_id"]],
                run_id=run["id"],
            ),
        )
        value = {
            **spec.model_dump(),
            "id": new_id(),
            "grant_id": grant["id"],
            "turns": 0,
            "connection": "connecting",
            "state": "starting",
            "observation": "runtime-public-events",
            "created_at": time.time(),
            "usage_status": "unknown-unless-reported",
        }
        wb.store.mutate(
            "runs",
            run["id"],
            lambda r: r.update(
                pilot_reserved_seconds=r.get("pilot_reserved_seconds", 0)
                + spec.max_seconds
            ),
        )
        wb.store.put("pilot_sessions", value["id"], value)
        session = Session(wb, value, grant["token"])
        _ACTIVE[value["id"]] = session
        session.messages.put(
            "Inspect the approved plan, prepare evidence and complete only the currently authorized pilot or remaining scope. Return artifact and review status."
        )
        threading.Thread(target=session.run, daemon=True).start()
        return value


def message(wb, id, text):
    with _LOCK:
        session = _ACTIVE.get(id)
    if not session:
        raise ValueError("Session is disconnected; resume the task first")
    if session.messages.qsize() >= 1:
        raise ValueError("A message is already queued; wait for the current turn")
    session.messages.put(text)
    return {"status": "queued"}


def control(wb, id, action):
    value = wb.store.get("pilot_sessions", id)
    if action not in {"pause", "cancel", "resume"}:
        raise ValueError("Unsupported control")
    with _LOCK:
        session = _ACTIVE.get(id)
    if action == "resume":
        if session:
            raise ValueError(
                "Session is still connected; wait for interruption to finish"
            )
        # A new conversation reuses durable task state, never assumes native resume.
        from .pilot_contracts import PilotStart

        return start(
            wb,
            PilotStart(
                run_id=value["run_id"],
                runtime=value["runtime"],
                max_turns=max(1, value["max_turns"] - value["turns"]),
                max_seconds=value["max_seconds"],
            ),
        )
    wb.store.mutate("grants", value["grant_id"], lambda g: g.update(enabled=False))
    if session:
        session.stop.set()
        if session.acp and session.remote_id:
            session.acp.send(
                {"method": "session/cancel", "params": {"sessionId": session.remote_id}}
            )
            threading.Thread(target=session.acp.close, daemon=True).start()
    if action == "cancel":
        wb.control(value["run_id"], "cancel")
    return wb.store.mutate(
        "pilot_sessions", id, lambda s: s.update(state=action + "_requested")
    )


def shutdown():
    with _LOCK:
        sessions = list(_ACTIVE.values())
    for session in sessions:
        session.stop.set()
        if session.acp:
            session.acp.close()


def recover(wb):
    """Core restart never pretends an old runtime process is still connected."""
    for session in wb.store.list("pilot_sessions"):
        if session["connection"] != "disconnected":
            wb.store.mutate(
                "pilot_sessions",
                session["id"],
                lambda s: s.update(connection="disconnected", state="interrupted"),
            )
            wb.store.mutate(
                "grants", session["grant_id"], lambda g: g.update(enabled=False)
            )
