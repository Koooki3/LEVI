"""The online judgement (interface C5): judge one episode while the
evaluation runs.

At the end of an episode the evaluation client sends the last seconds of
its side and wrist cameras (the frames the final-state spec names) to a
small HTTP endpoint of the live service's supervisor, which asks the local
model once with that spec and answers with a structured result. The client
writes the result into the rollout's ``metadata.json`` as
``eval.agent_label``; the mirror reads it back into the dataset state
(``criteria.agent_label``).

- **One job only.** ``GET /v1/judge/spec`` says which frames to send;
  ``POST /v1/judge`` judges one episode. No prompt is accepted: the question
  is the spec's, with the task instruction quoted the way the background
  review quotes it (``generic.anchored_spec``), in the lab's wording when
  ``[judge.task_text]`` has an entry for it (``generic.judge_task``).
- **Start frames are optional** (revision 2, a spec with a start check such
  as ``generic-final.v2``): ``GET /v1/judge/spec`` lists them under
  ``start_views`` (never under ``views``, which a client of revision 1 reads
  strictly), and a request that sends none is judged by the final frames
  alone, ``start_check`` ``skipped``.
- **Strict request.** A key the contract does not define (``operator``,
  ``outcome``, ``label``... or anything else) is refused with 422, so no
  operator or evaluation data can reach the model; ``episode`` goes to the
  log, and its ``task_folder`` is also looked up in ``[judge.task_text]`` on
  the server (the model sees the wording that entry gives, never the folder
  name). The model sees the images and the question, nothing else.
- **The same rule.** The answer is checked (``anchored.validate_answer``),
  read (``anchored.judge``) and turned into the outcome by the same
  ``anchored.outcome`` / ``anchored.undecided`` the background review uses.
- **The same model path.** ``LocalProvider.ask`` with the provider profile
  the worker builds (``Worker.ensure_provider``): the served model, guided
  decoding against the spec's answer schema, the spec's
  ``max_output_tokens``, the server's own greedy decoding.
- **Never in the policy's way.** The supervisor admits a request only when
  the GPU gate is open (read from the session files at that moment), vLLM is
  awake or may be woken under the GPU rules, and no other online judgement
  is running; otherwise the answer is ``unavailable`` at once. vLLM is never
  cold-started for it. A gate that closes while the model works cuts the
  request, as it cuts the worker's.

Every request adds one line to ``<workspace>/live/online.jsonl`` (no image,
no task text). Standard library at import: the heavy modules (pydantic, the
provider) load only when the endpoint is enabled.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ipaddress
import json
import math
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import generic, jsonio

REQUEST_SCHEMA = "levi.online.judge.request.v1"
RESULT_SCHEMA = "levi.online.judge.result.v1"
SPEC_SCHEMA = "levi.online.judge.spec.v1"
LOG_SCHEMA = "levi.live.online.v1"
# The interface revision (docs/LIVE.md "Interface C5"): 1 was the first;
# 2 adds ``start_views`` to the spec answer and ``start_check``,
# ``start_answer``, ``final_reading`` and ``task_rewritten`` to the result. The
# request keys and the three ``schema`` strings are the ones of revision 1.
REVISION = 2
LOG_FILE = "online.jsonl"
TMP_DIR = "online-tmp"
# What the client is asked for: JPEG, the camera's own frames, the longer
# side at most this (the background review sees native frames; a larger
# frame only costs tokens).
MAX_SIDE = 1280
OFFSET_TOLERANCE = 1e-3  # seconds: an image's offset_s matches a spec offset
MAX_TASK_CHARS = 2000
MAX_IMAGES = 64
# The request's keys, and nothing else.
TOP_KEYS = frozenset({"schema", "task", "episode", "images"})
REQUIRED_KEYS = frozenset({"schema", "task", "images"})
EPISODE_KEYS = {
    "group": str,
    "task_folder": str,
    "demo": str,
    "run_id": str,
    "steps": int,
    "fps": float,
}
IMAGE_KEYS = frozenset({"role", "offset_s", "step", "jpeg_b64"})
IMAGE_REQUIRED = frozenset({"role", "offset_s", "jpeg_b64"})
# Words that name operator or evaluation data: such a key is refused like any
# other undefined key, with a message that says why.
_EVAL_WORDS = (
    "operator",
    "outcome",
    "success",
    "fail",
    "label",
    "eval",
    "verdict",
    "truth",
    "score",
    "judg",
)
POLL_S = 0.1  # how often an answer in progress looks at the gate and the clock
GATE_WAIT_POLL_S = 0.1  # how often a request waiting for the gate asks again
GATE_WAIT_NOTE_S = 0.05  # a wait shorter than this is not mentioned
GATE_EVERY_S = 0.25


class Refused(Exception):
    """A request the endpoint does not take: ``code`` is the HTTP status."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass
class Request:
    task: str
    episode: dict
    # (role, offset_s, step, jpeg bytes) in the spec's order: its views, and
    # each view's offsets, as the background review shows them.
    images: list = field(default_factory=list)
    # The same for the spec's start views (the episode's first frames), in the
    # spec's order; empty when the client sent none (``start_check: skipped``).
    start_images: list = field(default_factory=list)


# --- the spec ---------------------------------------------------------------------


def spec_identity(name) -> dict:
    """``{"id", "version"}`` of a spec file, read with the standard library
    (the supervisor's status file names it)."""
    raw = json.loads(generic.text(name))
    return {"id": str(raw.get("id")), "version": int(raw.get("version") or 1)}


def load_spec(name):
    """The spec ``name`` (its question still holds ``{task}``) validated as an
    ``AnchoredSpec``; ValueError when it cannot be the online judgement's."""
    from levi.agent import anchored

    from .config import online_spec_problems

    problems = online_spec_problems(name)
    if problems:
        raise ValueError("; ".join(problems))
    return anchored.AnchoredSpec.model_validate(json.loads(generic.text(name)))


def views_of(spec) -> list:
    """``[(role, [offset seconds, ...])]`` in the spec's order."""
    return [(v.role, [float(x) for x in v.offsets_seconds]) for v in spec.views]


def start_views_of(spec) -> list:
    """The same for the spec's start check (the frames at the episode's
    start; offsets count from its first frame); empty without a start check
    that voids an episode."""
    if spec.start is None or not spec.start.void_when:
        return []
    return [(v.role, [float(x) for x in v.offsets_seconds]) for v in spec.start.views]


def spec_answer(spec, config) -> dict:
    """The body of ``GET /v1/judge/spec``. ``revision`` is the interface
    revision (2: optional start frames); the ``schema`` strings stay ``.v1``
    because a client of revision 1 checks them. ``start_views`` is present only
    for a spec with a start check; its roles are not in ``views``."""
    body = {
        "schema": SPEC_SCHEMA,
        "revision": REVISION,
        "spec_id": spec.id,
        "spec_version": spec.version,
        "views": [
            {"role": role, "offsets_seconds": offsets}
            for role, offsets in views_of(spec)
        ],
        "image": {"format": "jpeg", "max_side": MAX_SIDE},
        "timeout_s": config.online.timeout_s,
    }
    start = start_views_of(spec)
    if start:
        body["start_views"] = [
            {"role": role, "anchor": "start", "offsets_seconds": offsets}
            for role, offsets in start
        ]
    return body


# --- the request --------------------------------------------------------------------


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Refused(422, f"the key {key!r} appears twice")
        out[key] = value
    return out


def _no_constant(name):
    raise Refused(422, f"{name} is not a number this endpoint accepts")


def _undefined(keys, where) -> str:
    keys = sorted(str(k)[:40] for k in keys)
    flagged = [k for k in keys if any(w in k.lower() for w in _EVAL_WORDS)]
    text = f"{where} has keys the contract does not define: {', '.join(keys)}"
    if flagged:
        text += (
            " (operator or evaluation data is never accepted: the model sees "
            "only the images and the spec's question)"
        )
    return text


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse(body: bytes, spec) -> Request:
    """The request in ``body``, checked against the contract and matched to
    the spec's views; ``Refused`` (400 or 422) otherwise."""
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constant,
        )
    except Refused:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(400, f"the body is not JSON: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise Refused(422, "the body must be a JSON object")
    extra = set(value) - TOP_KEYS
    if extra:
        raise Refused(422, _undefined(extra, "the request"))
    missing = REQUIRED_KEYS - set(value)
    if missing:
        raise Refused(422, f"the request lacks {', '.join(sorted(missing))}")
    if value["schema"] != REQUEST_SCHEMA:
        raise Refused(422, f"schema must be {REQUEST_SCHEMA}")
    task = value["task"]
    if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
        raise Refused(
            422, f"task must be the instruction text (1-{MAX_TASK_CHARS} characters)"
        )
    episode = value.get("episode", {})
    if episode is None:
        episode = {}
    if not isinstance(episode, dict):
        raise Refused(422, "episode must be an object")
    extra = set(episode) - set(EPISODE_KEYS)
    if extra:
        raise Refused(422, _undefined(extra, "episode"))
    for key, kind in EPISODE_KEYS.items():
        item = episode.get(key)
        if item is None:
            continue
        ok = (
            _number(item)
            if kind is float
            else isinstance(item, int) and not isinstance(item, bool)
            if kind is int
            else isinstance(item, str) and len(item) <= 200
        )
        if not ok:
            raise Refused(422, f"episode.{key} has the wrong type")
    images = value["images"]
    if not isinstance(images, list) or not images or len(images) > MAX_IMAGES:
        raise Refused(422, f"images must be a list of 1-{MAX_IMAGES} images")
    wanted = views_of(spec)
    starts = start_views_of(spec)
    roles = {role: offsets for role, offsets in [*wanted, *starts]}
    found: dict = {}
    for n, item in enumerate(images):
        where = f"images[{n}]"
        if not isinstance(item, dict):
            raise Refused(422, f"{where} must be an object")
        extra = set(item) - IMAGE_KEYS
        if extra:
            raise Refused(422, _undefined(extra, where))
        lacking = IMAGE_REQUIRED - set(item)
        if lacking:
            raise Refused(422, f"{where} lacks {', '.join(sorted(lacking))}")
        role, offset, step = item["role"], item["offset_s"], item.get("step")
        if not isinstance(role, str) or role not in roles:
            raise Refused(
                422,
                f"{where}: role {str(role)[:40]!r} is not one of the spec's views "
                f"({', '.join(roles)})",
            )
        if not _number(offset) or not math.isfinite(offset):
            raise Refused(422, f"{where}: offset_s must be a number of seconds")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool)):
            raise Refused(422, f"{where}: step must be an integer or null")
        slot = next(
            (
                i
                for i, x in enumerate(roles[role])
                if abs(x - float(offset)) <= OFFSET_TOLERANCE
            ),
            None,
        )
        if slot is None:
            raise Refused(
                422,
                f"{where}: the spec shows {role} at {roles[role]} s, not at {offset} s",
            )
        if (role, slot) in found:
            raise Refused(
                422, f"{where}: a second image for {role} at {roles[role][slot]} s"
            )
        data = item["jpeg_b64"]
        if not isinstance(data, str):
            raise Refused(422, f"{where}: jpeg_b64 must be base64 text")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise Refused(422, f"{where}: jpeg_b64 is not valid base64") from None
        if not raw.startswith(b"\xff\xd8\xff"):
            raise Refused(422, f"{where}: the image is not a JPEG")
        found[(role, slot)] = (role, roles[role][slot], step, raw)
    missing = [
        f"{role} at {offset} s"
        for role, offsets in wanted
        for slot, offset in enumerate(offsets)
        if (role, slot) not in found
    ]
    if missing:
        raise Refused(422, "images missing for " + ", ".join(missing))
    ordered = [
        found[(role, slot)] for role, offsets in wanted for slot in range(len(offsets))
    ]
    # The start frames are all or none: none is a client that does not send
    # them (judged without the start check); some is a mistake.
    got = [
        (r, i) for r, offsets in starts for i in range(len(offsets)) if (r, i) in found
    ]
    lacking = [
        f"{role} at {offset} s"
        for role, offsets in starts
        for slot, offset in enumerate(offsets)
        if (role, slot) not in found
    ]
    if got and lacking:
        raise Refused(
            422,
            "start images missing for " + ", ".join(lacking) + " (send all of the "
            "spec's start views, or none)",
        )
    first = [
        found[(role, slot)]
        for role, offsets in starts
        for slot in range(len(offsets))
        if got
    ]
    clean = {k: episode.get(k) for k in EPISODE_KEYS if episode.get(k) is not None}
    return Request(task=task, episode=clean, images=ordered, start_images=first)


# --- the answer ---------------------------------------------------------------------


def result(
    status,
    *,
    reason=None,
    request_id=None,
    spec=None,
    model=None,
    elapsed=0.0,
    answer=None,
    checks=None,
    reading=None,
    outcome=None,
    undecided=False,
    tokens=None,
    prompt_tokens=None,
    final_reading=None,
    start_check=None,
    start_answer=None,
    task_rewritten=None,
) -> dict:
    """A ``levi.online.judge.result.v1`` body (every key, always). The last
    four keys are interface revision 2's: ``final_reading`` (the rule's
    reading of the episode: ``reading``, or the start check's decision over
    it), ``start_check`` (``null`` for a spec without a start check, else
    ``skipped``, ``passed``, ``voided`` or ``unclear``), ``start_answer`` and
    ``task_rewritten`` (``null``, ``task_text`` or ``task_folder``: the lab's
    wording of the instruction was quoted)."""
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "reason": reason,
        "outcome": outcome,
        "undecided": bool(undecided),
        "reading": reading,
        "answer": answer or {},
        "checks": checks or [],
        "spec": spec or {"id": None, "version": None},
        "model": model,
        "tokens": tokens,
        "prompt_tokens": prompt_tokens,
        "elapsed_s": round(float(elapsed), 3),
        "request_id": request_id,
        "final_reading": final_reading,
        "start_check": start_check,
        "start_answer": start_answer or {},
        "task_rewritten": task_rewritten,
    }


def decide(spec, answer, start_answer=None) -> dict:
    """The verdict on one validated answer, by the background review's own
    functions: the reading of ``valid_when`` (``anchored.judge``), the rule
    (``anchored.outcome``, here ``final_state``, with the start check's answer
    when the client sent the first frames) and ``anchored.undecided``."""
    from levi.agent import anchored

    checks, reading = anchored.judge(spec, answer)
    event = {
        "answer": answer,
        "checks": checks,
        "verdict": reading,
        "valid": reading == "supported",
    }
    outcome, basis = anchored.outcome(spec, [event], start_answer)
    return {
        "checks": checks,
        "reading": reading,
        "outcome": outcome,
        "undecided": anchored.undecided(outcome, basis),
        "basis": basis,
        "final_reading": basis.get("final_reading"),
        "start_check": basis.get("start_check"),
    }


class Judge:
    """One online judgement at a time.

    ``gpu`` is the supervisor (``Controller``): ``online_admit(now)`` returns
    ``(True, None, None)`` once the model may answer now (it wakes a sleeping
    vLLM when the GPU rules allow) or ``(False, code, reason)``;
    ``online_check(now)`` the same while the answer is in progress (the gate
    may close); ``online_done()`` ends what ``online_admit`` began;
    ``provider_spec()`` names the model server. ``ask`` replaces the model
    call in tests: ``ask(spec, folder, names) -> (raw answer, usage)``."""

    def __init__(self, config, gpu, *, ask=None, log=None):
        self.config = config
        self.gpu = gpu
        self.log = log or (lambda *a: None)
        self.spec = load_spec(config.online.spec)
        self.identity = {"id": self.spec.id, "version": self.spec.version}
        self._ask = ask or self._model
        self._busy = threading.Lock()
        self._owner = None
        self._stop_reason = None

    # --- the model call ---------------------------------------------------------

    def provider(self):
        """The provider profile of the model the supervisor serves, as the
        worker builds it (``Worker.ensure_provider``), bound to the digest the
        server reports now (in memory: nothing is written to the store)."""
        from levi.agent.schema import ProviderConfig
        from levi.inference.provider import client_for

        spec = self.gpu.provider_spec()
        fields = {
            "name": spec["name"],
            "kind": "openai-local",
            "base_url": spec["base_url"],
            "model": spec["model"],
            "context_tokens": spec["context_tokens"],
            "max_images": spec["max_images"],
            "prompt_style": self.config.provider.prompt_style,
            "requests_in_flight": self.config.provider.requests_in_flight,
            "allow_localhost": True,
            "vision": True,
            "structured_output": True,
        }
        bare = ProviderConfig.model_validate(fields)
        served = next(
            (m for m in client_for(bare, timeout=10).models() if m.name == bare.model),
            None,
        )
        if served is None:
            raise RuntimeError("the model server does not serve the configured model")
        return ProviderConfig.model_validate({**fields, "model_digest": served.digest})

    def _model(self, spec, folder, names):
        from levi.agent.schema import Budget
        from levi.inference.provider import LocalProvider

        config = self.provider()
        budget = Budget(
            max_calls=1,
            max_tokens=config.context_tokens,
            max_seconds=max(10, math.ceil(self.config.online.timeout_s) + 5),
        )
        return LocalProvider().ask(
            config,
            spec.question,
            names,
            folder,
            spec.answer_schema(),
            spec.max_output_tokens,
            budget,
        )

    # --- one request ------------------------------------------------------------

    def abort(self, reason) -> bool:
        """Cut the answer in progress (the supervisor puts vLLM to sleep or
        stops). True when there was one."""
        owner = self._owner
        if owner is None:
            return False
        self._stop_reason = reason
        with contextlib.suppress(Exception):
            from levi.inference import transport

            transport.abort(owner)
        return True

    def handle(self, body: bytes) -> tuple[int, dict]:
        """``(HTTP status, result body)`` for one ``POST /v1/judge``."""
        began = time.monotonic()
        request_id = uuid.uuid4().hex[:16]
        model = self.config.vllm.served_model
        episode: dict = {}

        def answer(code, status, images=0, **fields):
            body = result(
                status,
                request_id=request_id,
                spec=dict(self.identity),
                model=model,
                elapsed=time.monotonic() - began,
                **fields,
            )
            self.record(code, body, episode, images)
            return code, body

        try:
            request = parse(body, self.spec)
        except Refused as exc:
            return answer(exc.code, "error", reason=f"invalid_request: {exc}")
        episode = request.episode
        if not self._busy.acquire(blocking=False):
            return answer(
                200,
                "unavailable",
                reason="busy: another online judgement is in progress (one at a time)",
            )
        try:
            ok, code, why, waited = self._admit(began)
            if not ok:
                return answer(200, "unavailable", reason=f"{code}: {why}")
            try:
                found = self._run(request, request_id, began)
            finally:
                self.gpu.online_done()
            if waited and found.get("status") == "ok":
                found["reason"] = f"gate_waited: the gate opened after {waited:.1f} s"
            return answer(
                200,
                found.pop("status"),
                len(request.images) + len(request.start_images),
                **found,
            )
        except Exception as exc:  # noqa: BLE001 - the endpoint answers, always
            self.log(f"online judgement {request_id} failed: {exc!r}")
            return answer(200, "error", reason=f"internal_error: {type(exc).__name__}")
        finally:
            self._busy.release()

    def _admit(self, began):
        """``(ok, code, reason, seconds waited)``. A gate that is closed while
        no session is on the robot (``gate_pending``: the next episode is due,
        or a policy server no session vouches for) is waited for, at most
        until ``timeout_s`` after the request arrived; it ends at once when a
        session starts running (``gate_closed``). Everything else answers at
        once."""
        deadline = began + self.config.online.timeout_s
        while True:
            ok, code, why = self.gpu.online_admit(time.time())
            waited = time.monotonic() - began
            if ok:
                return True, None, None, waited if waited >= GATE_WAIT_NOTE_S else 0.0
            if code != "gate_pending":
                if waited >= GATE_WAIT_NOTE_S and code == "gate_closed":
                    why = f"{why} (after waiting {waited:.1f} s)"
                return False, code, why, waited
            if time.monotonic() + GATE_WAIT_POLL_S >= deadline:
                return (
                    False,
                    "gate_closed",
                    f"{why} (waited {waited:.1f} s for it to open)",
                    waited,
                )
            time.sleep(GATE_WAIT_POLL_S)

    def _run(self, request, request_id, began) -> dict:
        from levi.agent import anchored
        from levi.inference import transport

        # The spec's own question with the instruction quoted as the
        # background review quotes it (``generic.anchored_spec``), in the
        # lab's wording when ``[judge.task_text]`` has one (the client's
        # ``task`` is the policy's instruction and is never changed).
        owner = f"live-online:{request_id}"
        # Registered before anything else: a cut the supervisor asks for from
        # here on (``abort``) is seen, by the loop below and before the
        # request is sent.
        self._stop_reason = None
        self._owner = owner
        task, reworded = generic.judge_task(
            request.task,
            self.config.judge.task_text,
            request.episode.get("task_folder"),
        )
        spec = anchored.AnchoredSpec.model_validate(
            generic.anchored_spec(task, self.config.online.spec)
        )
        folder = self.config.live_dir / TMP_DIR / request_id
        folder.mkdir(parents=True, exist_ok=True)
        names, start_names = [], []
        for n, (role, _offset, _step, raw) in enumerate(request.images):
            name = f"{n:02d}-{role}.jpg"
            (folder / name).write_bytes(raw)
            names.append(name)
        for n, (role, _offset, _step, raw) in enumerate(request.start_images):
            name = f"start-{n:02d}-{role}.jpg"
            (folder / name).write_bytes(raw)
            start_names.append(name)
        box: dict = {}
        done = threading.Event()

        def work():
            try:
                with transport.requests_of(owner):
                    # ``requests_of`` forgets a cut made before it: look again.
                    # The start check is asked first (two small images); the
                    # final frames' question is the one the result rests on.
                    if self._stop_reason is None and start_names:
                        box["start_raw"], box["start_usage"] = self._ask(
                            spec.start, folder, start_names
                        )
                    if self._stop_reason is None:
                        box["raw"], box["usage"] = self._ask(spec, folder, names)
            except BaseException as exc:  # noqa: BLE001 - reported below
                box["error"] = exc
            finally:
                shutil.rmtree(folder, ignore_errors=True)
                done.set()

        thread = threading.Thread(target=work, name="live-online-ask", daemon=True)
        thread.start()
        deadline = began + self.config.online.timeout_s
        looked = 0.0
        try:
            while not done.wait(POLL_S):
                now = time.monotonic()
                if now >= deadline:
                    transport.abort(owner)
                    return {
                        "status": "error",
                        "reason": f"timeout: no answer within {self.config.online.timeout_s:g} s",
                    }
                if self._stop_reason:
                    transport.abort(owner)
                    return {"status": "unavailable", "reason": self._stop_reason}
                if now - looked >= GATE_EVERY_S:
                    looked = now
                    ok, code, why = self.gpu.online_check(time.time())
                    if not ok:
                        transport.abort(owner)
                        return {
                            "status": "unavailable",
                            "reason": f"{code}: {why} (the request was cut)",
                        }
        finally:
            self._owner = None
        if self._stop_reason:
            return {"status": "unavailable", "reason": self._stop_reason}
        if "error" in box:
            exc = box["error"]
            from levi.inference.gpu import GpuBusy

            if isinstance(exc, GpuBusy):
                return {"status": "unavailable", "reason": f"gate_closed: {exc}"}
            return {
                "status": "error",
                "reason": f"model_error: {type(exc).__name__}: {str(exc)[:200]}",
            }

        # What the server reported, over both questions when two were asked.
        def spent(key):
            seen = [
                u.get(key)
                for u in (box.get("start_usage") or {}, box.get("usage") or {})
                if u.get(key) is not None
            ]
            return sum(seen) if seen else None

        tokens, prompt = spent("reported_tokens"), spent("prompt_tokens")
        start_answer = None
        if start_names:
            try:
                start_answer = anchored.validate_answer(
                    spec.start, box.get("start_raw") or ""
                )
            except ValueError as exc:
                return {
                    "status": "error",
                    "reason": f"invalid_answer: start check: {exc}",
                    "tokens": tokens,
                    "prompt_tokens": prompt,
                    "task_rewritten": reworded,
                }
        try:
            answer = anchored.validate_answer(spec, box.get("raw") or "")
        except ValueError as exc:
            return {
                "status": "error",
                "reason": f"invalid_answer: {exc}",
                "tokens": tokens,
                "prompt_tokens": prompt,
                "task_rewritten": reworded,
            }
        verdict = decide(spec, answer, start_answer)
        return {
            "status": "ok",
            "answer": answer,
            "checks": verdict["checks"],
            "reading": verdict["reading"],
            "outcome": verdict["outcome"],
            "undecided": verdict["undecided"],
            "final_reading": verdict["final_reading"],
            "start_check": verdict["start_check"],
            "start_answer": start_answer,
            "task_rewritten": reworded,
            "tokens": tokens,
            "prompt_tokens": prompt,
        }

    # --- the log ----------------------------------------------------------------

    def record(self, code, body, episode, images):
        """One line in ``live/online.jsonl``: when, which episode, what came
        back. No image and no task text."""
        r = self.config.resources
        row = {
            "schema": LOG_SCHEMA,
            "at": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request_id": body["request_id"],
            "http": code,
            "episode": {k: episode.get(k) for k in EPISODE_KEYS if k in episode},
            "status": body["status"],
            "reason": (body["reason"] or None) and str(body["reason"])[:300],
            "outcome": body["outcome"],
            "undecided": body["undecided"],
            "reading": body["reading"],
            "final_reading": body["final_reading"],
            "start_check": body["start_check"],
            "task_rewritten": body["task_rewritten"],
            "tokens": body["tokens"],
            "prompt_tokens": body["prompt_tokens"],
            "elapsed_s": body["elapsed_s"],
            "images": images,
            "spec": body["spec"],
            "model": body["model"],
        }
        with contextlib.suppress(OSError, TypeError, ValueError):
            jsonio.append_line(
                self.config.live_dir / LOG_FILE,
                row,
                max_bytes=r.log_max_mb * 1024 * 1024,
                keep=r.log_backups,
            )


def read_log(live_dir, limit=None) -> list:
    """The lines of ``online.jsonl`` (oldest first; damaged lines skipped)."""
    rows = []
    try:
        lines = (Path(live_dir) / LOG_FILE).read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows[-limit:] if limit else rows


# --- the HTTP endpoint ----------------------------------------------------------------


def _hosts(host, port) -> set:
    """The ``Host`` headers a loopback client sends (a page in a browser that
    re-binds a name to 127.0.0.1 sends its own name and is refused)."""
    names = {host, "localhost", "127.0.0.1", "[::1]"}
    if ":" in host and not host.startswith("["):
        names.add(f"[{host}]")
    return {f"{n}:{port}" for n in names} | names


def handler_for(judge, host, port, max_bytes):
    allowed = _hosts(host, port)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30  # a client that stops sending does not hold a thread
        server_version = "levi-online"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def _refuse(self, code, reason):
            # Not a judgement: the request never reached the model.
            body = result(
                "error",
                reason=f"invalid_request: {reason}",
                spec=dict(judge.identity),
                model=judge.config.vllm.served_model,
            )
            judge.record(code, body, {}, 0)
            self._send(code, body)

        def _origin_ok(self) -> str | None:
            if (self.headers.get("Host") or "") not in allowed:
                return "the Host header is not a loopback address of this endpoint"
            if self.headers.get("Origin"):
                return "requests from a web page are not accepted"
            return None

        def do_GET(self):
            problem = self._origin_ok()
            if problem:
                self._send(403, {"error": problem})
            elif self.path == "/v1/judge/spec":
                self._send(200, spec_answer(judge.spec, judge.config))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/judge":
                self._send(404, {"error": "not found"})
                return
            problem = self._origin_ok()
            if problem:
                self._refuse(403, problem)
                return
            kind = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if kind.lower() != "application/json":
                self._refuse(415, "Content-Type must be application/json")
                return
            if self.headers.get("Transfer-Encoding"):
                self._refuse(411, "send the body with a Content-Length")
                return
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self._refuse(411, "a Content-Length is required")
                return
            if length < 0:
                self._refuse(400, "a negative Content-Length")
                return
            if length > max_bytes:
                self._refuse(
                    413,
                    f"the body is {length} bytes, over online.max_body_mb "
                    f"({max_bytes} bytes)",
                )
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._refuse(400, "the body ended before its Content-Length")
                return
            code, value = judge.handle(body)
            self._send(code, value)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        host = address[0]
        if ipaddress.ip_address(host).version == 6:
            import socket

            self.address_family = socket.AF_INET6
        super().__init__(address, handler)


class Endpoint:
    """The online judgement's HTTP endpoint, a thread of the supervisor."""

    def __init__(self, config, gpu, *, ask=None, log=None):
        self.config = config
        self.log = log or (lambda *a: None)
        self.judge = Judge(config, gpu, ask=ask, log=self.log)
        self.server = None
        self.thread = None
        self.error = ""

    @property
    def url(self) -> str:
        return url_of(self.config)

    @property
    def listening(self) -> bool:
        return (
            self.server is not None
            and self.thread is not None
            and (self.thread.is_alive())
        )

    def start(self) -> bool:
        o = self.config.online
        if not ipaddress.ip_address(o.host).is_loopback:  # (validated before)
            raise ValueError("online.host must be a loopback address")
        shutil.rmtree(self.config.live_dir / TMP_DIR, ignore_errors=True)
        # Loaded now, not by the first request (it must answer quickly).
        import importlib

        for module in ("gpu", "provider", "transport"):
            importlib.import_module(f"levi.inference.{module}")

        handler = handler_for(
            self.judge, o.host, o.port, int(o.max_body_mb * 1024 * 1024)
        )
        try:
            self.server = Server((o.host, o.port), handler)
        except OSError as exc:
            self.error = f"cannot listen on {o.host}:{o.port}: {exc.strerror or exc}"
            self.server = None
            return False
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="live-online",
            daemon=True,
        )
        self.thread.start()
        self.error = ""
        return True

    def stop(self):
        self.judge.abort("shutting_down: the live service is stopping")
        if self.server is not None:
            with contextlib.suppress(Exception):
                self.server.shutdown()
            with contextlib.suppress(Exception):
                self.server.server_close()
        self.server = None
        shutil.rmtree(self.config.live_dir / TMP_DIR, ignore_errors=True)


def url_of(config) -> str:
    host = config.online.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.online.port}"


# --- background labelling off ----------------------------------------------------------


def stats_record(config, name, demo, row, now) -> dict:
    """The ``live/stats.jsonl`` record of an episode taken in while the
    background labelling is off (``pipeline.background = false``): its
    timeline, the online judgement's cost (one request, two when the client
    sent the first frames for a start check) when the client relayed one, its
    verdict or none, and the operator label."""
    from . import exclusion, stats

    online = row.get("online") or {}
    verdict = (
        row.get("verdict")
        if (row.get("verdict") or {}).get("source") == "online"
        else None
    )
    base = row.get("completed_at")
    usage = online.get("usage") or {}
    asked = online.get("status") in ("ok", "error", "timeout")
    asked_start = int(
        stats.reading_of(verdict).get("start_check") not in (None, "skipped")
    )
    tokens = usage.get("tokens")
    prompt = usage.get("prompt_tokens")
    completion = (
        tokens - prompt
        if isinstance(tokens, int) and isinstance(prompt, int) and tokens >= prompt
        else None
    )
    record = {
        "schema": stats.SCHEMA,
        "at": round(now, 3),
        "dataset": name,
        "demo": demo,
        "episode_index": row.get("episode_index"),
        "session": row.get("run_id"),
        "attempts": 0,
        "excluded": exclusion.is_excluded(row),
        "batch": {"id": None, "size": None},
        "timeline": {
            "to_mirror_s": stats.after(row.get("mirrored_at"), base),
            "to_verdict_s": stats.after((verdict or {}).get("at"), base),
            "completed_at": base,
        },
        "model": {
            # The final question, and the start check when the client sent the
            # first frames (``basis.start_check`` other than ``skipped``).
            "requests": {"review": (1 + asked_start) if asked else 0},
            "model_seconds": {"review": usage.get("elapsed_s") if asked else None},
            "tokens": {"review": tokens},
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": tokens if completion is not None else None,
            "images": usage.get("images"),
            "external_tokens": 0,
        },
        "result": {
            "state": row.get("state"),
            "reason": (str(row["reason"])[:200] if row.get("reason") else None),
            "verdict": {
                k: verdict.get(k)
                for k in ("outcome", "events", "valid_events", "undecided", "rule")
            }
            | {"source": "online"}
            | stats.reading_of(verdict)
            if verdict
            else None,
            "review": "auto" if verdict else None,
            "spec": {
                "guideline": None,
                "release_review": (verdict or {}).get("spec"),
                "release_review_version": (verdict or {}).get("spec_version"),
                "sha256": {},
            },
            "provider": None,
            "model": usage.get("model"),
            "task_rewritten": (verdict or {}).get("task_rewritten"),
            "online": {
                "status": online.get("status"),
                "reason": online.get("reason"),
                "timing": online.get("timing"),
            }
            if online
            else None,
            "background": False,
        },
        "operator_label": stats.operator_brief(row.get("operator_label")),
    }
    return record


def take_in(config, name, demos, now) -> list:
    """With the background labelling off: the demos just mirrored are done.
    Each keeps the online verdict the mirror read (``criteria.agent_label``)
    or, without one, none (``no_agent``: the reason says why), and gets a
    statistics record. Returns the demos taken in."""
    from . import mirror, stats

    path = mirror.state_path(config, name)
    taken = []

    def change(value):
        for demo in demos:
            row = (value.get("demos") or {}).get(demo)
            if not row or row.get("state") != "mirrored" or row.get("excluded"):
                continue
            row["state"] = "done"
            row["taken_in"] = {"at": now, "background": False}
            online = row.get("online")
            if not row.get("verdict"):
                row["reason"] = (
                    "no automatic label: background labelling is off and "
                    + (
                        f"the online judgement was {online.get('status')}"
                        + (f" ({online.get('reason')})" if online.get("reason") else "")
                        if online
                        else "the client relayed no online judgement"
                    )
                )[:300]
            taken.append(demo)
        if taken:
            value["last_processed_at"] = now
        return value

    jsonio.update(path, change, default=dict)
    state = mirror.load_state(config, name) or {}
    r = config.resources
    for demo in taken:
        row = (state.get("demos") or {}).get(demo) or {}
        with contextlib.suppress(Exception):
            stats.record(
                config.live_dir,
                stats_record(config, name, demo, row, now),
                r.log_max_mb * 1024 * 1024,
                r.log_backups,
            )
    return taken
