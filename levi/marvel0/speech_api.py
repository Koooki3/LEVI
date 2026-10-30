"""Voice input for the client command card of the marvel0 page:

- ``GET /api/levi/marvel0/speech``: whether speech is installed, what the
  worker is doing, and whether it may run now (``gate``). Passive: it never
  starts the worker;
- ``POST /api/levi/marvel0/speech/transcribe``: one WAV recording (16 kHz mono
  16-bit, at most 10 s) in, the transcript out, with the closest of the allowed
  task instructions (``speech_match.py``);
- ``POST /api/levi/marvel0/speech/match``: the same matching for a transcript
  the person edited. Text only; the worker is not touched;
- ``POST /api/levi/marvel0/speech/release``: stop the worker (idempotent).

**A transcript is a proposal and nothing else.** The page shows it, the person
clicks Use, and only a sentence from the allowed list (the site file's known
instructions; for a restricted model the two it was trained on) can reach the
command card's task instruction. Nothing here builds a command, starts the
client or the arm, or says anything about stopping the robot: speech does not
stop it, the physical e-stop does.

All four routes are a person's (``person_only``) and need the site file first
(412 without it). Errors have the shape of ``errors.fail``; ``SPEECH_REFUSALS``
lists the codes of this module. **The audio exists only in memory and in the
worker's pipe; the transcript only in the answer to the page.** Neither is
written to a file, an error, a URL or a log line: the log gets the request id,
an outcome word, byte counts and eight characters of an HMAC whose key is made
at random when the process starts (lines of one run can be compared; a known
sentence cannot be looked up).

Guards (``gate``) are read from the policy deploy's passive status (cached for
two seconds): a policy server that is starting or in an unknown state, a deploy
or stop in progress (``operation``, read live), a client connected to it, the
AERI arm lock held, and, while the worker is not loaded, too little free GPU
memory all refuse a transcription. The memory part is asked again, under the
manager's request lock, when the worker turns out to need a cold start
(``may_start``), and the manager's idle watcher asks the other parts every two
seconds and releases a loaded worker once they close (``set_guard``). The
physical GPU lock is not taken (it would stop a policy deploy), and
``levi/inference/gpu.py`` is not used (its quiet-GPU rule would refuse exactly
while the policy server runs).
"""

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from levi.stt import manager as stt_manager
from levi.stt import settings as stt_settings
from levi.stt import wav as stt_wav
from levi.stt.errors import SttError

from . import deploy_api, models
from . import site as marvel0_site
from .deploy_api import person_only
from .errors import fail
from .record import REQUEST_ID

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])
log = logging.getLogger("levi.marvel0.speech")

STATUS_SCHEMA = "levi.marvel0.speech.status.v1"
RESULT_SCHEMA = "levi.marvel0.speech.v1"
MATCH_SCHEMA = "levi.marvel0.speech.match.v1"
MATCH_MAX_BYTES = 8 * 1024
MATCH_MAX_CHARS = 500
MODEL_ID_MAX = 120

# code -> (HTTP status, message). The messages hold no path and no transcript;
# a few take the facts of the refusal (the extra keys) through ``{}`` fields.
SPEECH_REFUSALS = {
    "request_id_invalid": (
        400,
        "request_id is 8-80 characters of A-Z, a-z, 0-9 and . _ : -",
    ),
    "speech_body_invalid": (400, "The request body is not valid"),
    "audio_too_short": (400, "The recording is shorter than 0.3 seconds"),
    "audio_too_large": (
        413,
        "The recording is longer than {max_seconds} seconds or larger than {max_bytes} bytes",
    ),
    "audio_format": (
        415,
        "Send 16 kHz mono 16-bit PCM WAV audio with Content-Type audio/wav",
    ),
    "speech_not_installed": (503, "The speech worker is not installed on this machine"),
    "speech_model_missing": (503, "The speech model is not installed"),
    "speech_manifest_mismatch": (
        503,
        "The speech model files do not match their manifest",
    ),
    "speech_gpu_unreadable": (503, "The GPU's memory use could not be read"),
    "speech_policy_starting": (
        409,
        "The policy server is starting or its state is unknown; try again when it has settled",
    ),
    "speech_client_connected": (
        409,
        "A client is connected to the policy server; speech input is off while it runs",
    ),
    "speech_arm_in_use": (
        409,
        "The arm lock is held or could not be read; speech input is off while a run uses the arm",
    ),
    "speech_gpu_insufficient": (
        409,
        (
            "{free_mib} MiB of GPU memory are free; the speech model needs "
            "{required_mib} MiB ({reserved_mib} + {margin_mib})"
        ),
    ),
    "speech_allowed_set_invalid": (
        412,
        "The known task instructions of the site file cannot be told apart reliably",
    ),
    "speech_busy": (
        429,
        "One transcription at a time, and at most {limit} a minute; try again shortly",
    ),
    "speech_failed": (502, "The speech worker failed; try again"),
    "speech_timeout": (504, "The speech worker did not answer in time"),
}

# The limits a page may show with these refusals.
FACTS = {
    "audio_too_large": {
        "max_seconds": stt_settings.MAX_SECONDS,
        "max_bytes": stt_settings.MAX_BYTES,
    },
    "speech_busy": {"limit": stt_settings.RATE_PER_MINUTE},
}
# gate reason -> refusal code, in the order the first one is reported
GATE_CODES = {
    "policy_starting": "speech_policy_starting",
    "client_connected": "speech_client_connected",
    "arm_in_use": "speech_arm_in_use",
    "gpu_insufficient": "speech_gpu_insufficient",
    "gpu_unreadable": "speech_gpu_unreadable",
}
# SttError code -> refusal code
STT_CODES = {
    "busy": "speech_busy",
    "not_installed": "speech_not_installed",
    "model_missing": "speech_model_missing",
    "manifest_mismatch": "speech_manifest_mismatch",
    "timeout": "speech_timeout",
    "audio_invalid": "audio_format",
    "audio_too_long": "audio_too_large",
    # A policy deploy holds the manager off (``Manager.held_off``).
    "held": "speech_policy_starting",
}
WAV_CODES = {
    "format": "audio_format",
    "too_short": "audio_too_short",
    "too_long": "audio_too_large",
    "too_large": "audio_too_large",
}


def manager() -> stt_manager.Manager:
    """The process's speech worker manager, with this module's gate guard
    registered (tests replace this function)."""
    mgr = stt_manager.get()
    mgr.set_guard(_guard_closed)
    return mgr


def refuse(code: str, **extra) -> HTTPException:
    """The refusal ``code`` as an ``HTTPException``; the facts that belong to
    it (the limits) are added, and ``extra`` says more."""
    status, text = SPEECH_REFUSALS[code]
    extra = {**FACTS.get(code, {}), **extra}
    try:
        message = text.format(**extra)
    except (KeyError, IndexError):
        message = text
    return fail(status, code, message, **extra)


class RateLimit:
    """At most ``limit`` takes in any ``window`` seconds, in this process."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def take(self) -> float:
        """0.0 when allowed (and counted); else the seconds until a slot frees."""
        with self._lock:
            now = self.clock()
            while self._hits and now - self._hits[0] >= self.window:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return max(0.1, self.window - (now - self._hits[0]))
            self._hits.append(now)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


RATE = RateLimit(stt_settings.RATE_PER_MINUTE, 60.0)


# Made once per process and never stored: the fingerprints of one run can be
# compared with each other, but nobody can hash the known instructions and
# look up which one was said.
_AUDIT_KEY = secrets.token_bytes(32)


def _fingerprint(data: bytes) -> str:
    return hmac.new(_AUDIT_KEY, data, hashlib.sha256).hexdigest()[:8]


def _audit(request_id: str, outcome: str, **facts: int | str) -> None:
    """One log line. Only the request id (validated), a code and numbers or
    hash prefixes: never a transcript, never audio."""
    detail = " ".join(f"{key}={value}" for key, value in facts.items())
    log.info("speech request_id=%s outcome=%s %s", request_id, outcome, detail)


# ---------------------------------------------------------------- the guards


def gate(site, status, loaded: bool) -> list:
    """Why a transcription may not run now: ``[(reason, extra)]`` in the order
    they are reported (empty when it may). ``status`` is the policy deploy's
    ``levi.marvel0.deploy.v1`` document, or ``None`` when it could not be read:
    unknown counts as a policy server that is starting. ``loaded`` says the
    worker already holds its GPU memory, so the memory check is skipped."""
    if not isinstance(status, dict):
        return [("policy_starting", {})]
    reasons: list = []
    listener = status.get("listener")
    established = listener.get("established") if isinstance(listener, dict) else None
    unsettled = status.get("state") in ("starting", "unknown") or not isinstance(
        status.get("state"), str
    )
    # A deploy or a stop in progress: ``operation`` is filled live (outside
    # the status cache) from the start of the transaction, while ``state`` can
    # still say ``idle`` until the intent record is written.
    if status.get("operation") is not None:
        unsettled = True
    if isinstance(established, bool) or not isinstance(established, int):
        unsettled = True  # a client cannot be ruled out
    if unsettled:
        reasons.append(("policy_starting", {}))
    elif established > 0:
        reasons.append(("client_connected", {"established": established}))
    # The arm lock row of the deploy's checks is its own ``_check_arm``: held or
    # unreadable is "in use", and so is a row that is missing.
    row = next(
        (
            c
            for c in status.get("checks") or ()
            if isinstance(c, dict) and c.get("code") == "arm_lock"
        ),
        None,
    )
    if row is None or row.get("level") != "ok":
        reasons.append(("arm_in_use", {}))
    if not loaded:
        reasons += _gpu_reasons(site, status.get("gpu"))
    return reasons


def _gpu_reasons(site, gpu) -> list:
    free = gpu.get("free_mib") if isinstance(gpu, dict) else None
    if (
        not isinstance(gpu, dict)
        or gpu.get("state") != "ok"
        or isinstance(free, bool)
        or not isinstance(free, int)
    ):
        return [("gpu_unreadable", {})]
    margin = site.gpu.margin_mib
    needed = stt_settings.STT_VRAM_MIB + margin
    if free < needed:
        return [
            (
                "gpu_insufficient",
                {
                    "free_mib": free,
                    "required_mib": needed,
                    "reserved_mib": stt_settings.STT_VRAM_MIB,
                    "margin_mib": margin,
                },
            )
        ]
    return []


def _policy_status():
    """The policy deploy's passive status (cached two seconds), or ``None``."""
    try:
        return deploy_api.supervisor().status()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable is "unknown", never a 500
        log.warning("the policy status could not be read (%s)", type(exc).__name__)
        return None


def _loaded(state: dict) -> bool:
    return state["state"] in ("loading", "ready", "busy")


def _guard_closed() -> bool:
    """For the manager's idle watcher: whether a loaded worker must go because
    the gate closed (a policy server starting, a client connected, the arm
    lock held). The memory part is not asked: the worker already holds its
    memory. A site file that cannot be read raises, which counts as closed."""
    return bool(gate(marvel0_site.require(), _policy_status(), loaded=True))


def _cold_start_check(site):
    """``may_start`` for ``Manager.transcribe``: the whole gate, read again
    without a loaded worker, right before a worker is started."""

    def check() -> None:
        blocked = gate(site, _policy_status(), loaded=False)
        if blocked:
            reason, extra = blocked[0]
            raise refuse(GATE_CODES[reason], reasons=[r for r, _ in blocked], **extra)

    return check


# ---------------------------------------------------------------- matching


def _matcher():
    # Imported on first use: the service starts, and every other marvel0 route
    # works, without loading the matcher and its lexicon.
    from . import speech_match

    return speech_match


def _allowed(site, model_id) -> tuple:
    """``(allowed sentences, family)``: the two a restricted model was trained
    on, else the site file's known task instructions."""
    allowed, family = tuple(site.prompts.known), "all"
    if model_id is not None:
        model = models.get(site, model_id)
        if model is None:
            raise fail(
                422, "model_unknown", "this model is not offered", field="model_id"
            )
        if model.restricted:
            allowed, family = tuple(models.RESTRICTED_PROMPTS), "restricted"
    try:
        problems = _matcher().check_allowed_set(allowed)
    except ValueError as exc:
        log.warning("the allowed set was refused (%s)", type(exc).__name__)
        raise refuse("speech_allowed_set_invalid") from None
    if isinstance(problems, (list, tuple, set, frozenset)) and problems:
        log.warning("the allowed set was refused (%d problems)", len(problems))
        raise refuse("speech_allowed_set_invalid")
    return allowed, family


def _allowed_revision(allowed, family: str) -> str:
    payload = json.dumps(
        [family, list(allowed)], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _match(transcript: str, allowed: tuple, family: str):
    try:
        result = _matcher().match_transcript(transcript, allowed, family=family)
        return {
            **result,
            "allowed_revision": _allowed_revision(allowed, family),
            "generated": _generated(transcript, family),
        }
    except Exception as exc:  # noqa: BLE001 - the text is not in the log, only the kind
        log.warning("matching failed (%s)", type(exc).__name__)
        raise refuse("speech_failed") from None


# ------------------------------------------------------------ generated ----
# The datasets' instructions with their episode counts, read once per catalog
# revision: the frames a spoken task is written in (speech_template).
_frames_lock = threading.Lock()
_frames_cache: tuple[tuple, object] | None = None


def _dataset_instructions() -> tuple[tuple, dict[str, int]]:
    """``(catalog key, {instruction: episodes})`` over the registered datasets
    (a namespace shares its base's episodes and is not counted twice)."""
    from pathlib import Path

    from levi import catalog

    rows: list[tuple[str, int]] = []
    key = []
    for name, entry in sorted(catalog.datasets().items()):
        if entry.get("base"):
            continue
        root = entry.get("view") or entry.get("path")
        if not root:
            continue
        key.append((name, str(entry.get("revision"))))
        rows.extend(_episode_tasks(Path(root)))
    from . import speech_template

    return tuple(key), speech_template.counts_from(rows)


def _episode_tasks(root) -> list[tuple[str, int]]:
    """(instruction, episodes) of one dataset; an unreadable one gives none."""
    from collections import Counter

    counts: Counter[str] = Counter()
    try:
        episodes = root / "meta/episodes.jsonl"
        if episodes.is_file():
            with episodes.open(encoding="utf-8") as handle:
                for line in handle:
                    tasks = json.loads(line).get("tasks") if line.strip() else None
                    for task in tasks if isinstance(tasks, list) else ():
                        if isinstance(task, str):
                            counts[task] += 1
            return list(counts.items())
        files = sorted((root / "meta/episodes").glob("chunk-*/file-*.parquet"))
        if files:
            import pyarrow.parquet as pq

            for file in files:
                table = pq.read_table(file, columns=["tasks"])
                for tasks in table.column("tasks").to_pylist():
                    for task in tasks or ():
                        if isinstance(task, str):
                            counts[task] += 1
    except (OSError, ValueError, KeyError) as exc:
        log.warning("a dataset's instructions were not read (%s)", type(exc).__name__)
        return []
    return list(counts.items())


def _frames():
    global _frames_cache
    from levi import catalog

    from . import speech_template

    key = tuple(
        (name, str(entry.get("revision")))
        for name, entry in sorted(catalog.datasets().items())
        if not entry.get("base")
    )
    with _frames_lock:
        if _frames_cache is not None and _frames_cache[0] == key:
            return _frames_cache[1]
    key, counts = _dataset_instructions()
    frames = speech_template.learn_frames(counts)
    with _frames_lock:
        _frames_cache = (key, frames)
    return frames


def _generated(transcript: str, family: str) -> dict:
    """A sentence in the datasets' most common phrasing (labelled generated),
    or the reason there is none. Its failure never fails the match."""
    from . import speech_template

    try:
        return speech_template.generate(transcript, _frames(), family=family)
    except Exception as exc:  # noqa: BLE001 - the match stands without it
        log.warning("generation failed (%s)", type(exc).__name__)
        return {
            "status": "no_frame",
            "prompt": None,
            "frames": [],
            "episodes": 0,
            "slots": [],
            "untranslated": [],
            "dataset_original": None,
        }


# ---------------------------------------------------------------- reading a request


def _declared_over(request: Request, limit: int) -> bool:
    declared = request.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > limit


async def _read_limited(request: Request, limit: int, code: str) -> bytes:
    """The body, at most ``limit`` bytes: a larger ``Content-Length`` is refused
    before anything is read, and the stream is counted as it arrives, so a
    sender that lies or sends no length is stopped at the cap too."""
    if _declared_over(request, limit):
        raise refuse(code)
    chunks, total = [], 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise refuse(code)
            chunks.append(chunk)
    except ClientDisconnect:
        raise refuse("speech_body_invalid") from None
    return b"".join(chunks)


# ---------------------------------------------------------------- routes


def _view(site, model_id=None) -> dict:
    mgr = manager()
    state = mgr.status()
    present = mgr.installed()
    reasons = [r for r, _ in gate(site, _policy_status(), _loaded(state))]
    revision = None
    try:
        allowed, family = _allowed(site, model_id)
        revision = _allowed_revision(allowed, family)
    except HTTPException as exc:
        if exc.detail.get("code") != "speech_allowed_set_invalid":
            raise
        reasons.append("allowed_set_invalid")
    shown, reason = state["state"], state["reason"]
    if not (present.worker and present.model):
        code = "speech_not_installed" if not present.worker else "speech_model_missing"
        shown, reason = "unavailable", {"code": code, "text": SPEECH_REFUSALS[code][1]}
    return {
        "schema": STATUS_SCHEMA,
        "allowed_revision": revision,
        "state": shown,
        "reason": reason,
        "installed": {"worker": present.worker, "model": present.model},
        "model": state["model"],
        "device": state["device"],
        "limits": {
            "max_seconds": stt_settings.MAX_SECONDS,
            "max_bytes": stt_settings.MAX_BYTES,
        },
        "idle_seconds": stt_settings.IDLE_SECONDS,
        "gate": {"allowed": not reasons, "reasons": reasons},
    }


@router.get("/speech", dependencies=[Depends(person_only)])
def speech_status(model_id: str | None = None):
    """What speech input can do now (for the model of the form, when
    ``model_id`` is given). Passive: starts nothing."""
    if model_id is not None and len(model_id) > MODEL_ID_MAX:
        raise fail(422, "model_unknown", "this model is not offered", field="model_id")
    return _view(marvel0_site.require(), model_id or None)


@router.post("/speech/release", dependencies=[Depends(person_only)])
def speech_release():
    """Stop the speech worker and free its GPU memory (idempotent)."""
    marvel0_site.require()
    manager().release()
    return {"state": "off", "released": True}


@router.post("/speech/match", dependencies=[Depends(person_only)])
async def speech_match_text(request: Request):
    """Match a transcript the person edited. Text only: no worker, no audio."""
    site = await run_in_threadpool(marvel0_site.require)
    raw = await _read_limited(request, MATCH_MAX_BYTES, "speech_body_invalid")
    try:
        body = json.loads(raw)
    except ValueError:
        raise refuse("speech_body_invalid") from None
    transcript = body.get("transcript") if isinstance(body, dict) else None
    model_id = body.get("model_id") if isinstance(body, dict) else None
    if (
        not isinstance(transcript, str)
        or not transcript.strip()
        or len(transcript) > MATCH_MAX_CHARS
        or set(body) - {"transcript", "model_id"}
        or not (model_id is None or isinstance(model_id, str))
        or (model_id is not None and len(model_id) > MODEL_ID_MAX)
    ):
        raise refuse("speech_body_invalid")
    return await run_in_threadpool(_match_text, site, transcript, model_id or None)


def _match_text(site, transcript: str, model_id) -> dict:
    allowed, family = _allowed(site, model_id)
    return {"schema": MATCH_SCHEMA, "match": _match(transcript, allowed, family)}


@router.post("/speech/transcribe", dependencies=[Depends(person_only)])
async def speech_transcribe(request: Request):
    """One recording in, the transcript and the closest allowed instruction out."""
    site = await run_in_threadpool(marvel0_site.require)
    request_id = request.query_params.get("request_id", "")
    if not REQUEST_ID.fullmatch(request_id):
        raise refuse("request_id_invalid")
    model_id = request.query_params.get("model_id") or None
    if model_id is not None and len(model_id) > MODEL_ID_MAX:
        raise fail(422, "model_unknown", "this model is not offered", field="model_id")
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != stt_wav.CONTENT_TYPE:
        _audit(request_id, "audio_format")
        raise refuse("audio_format")
    if _declared_over(request, stt_settings.MAX_BYTES):
        _audit(request_id, "audio_too_large")
        raise refuse("audio_too_large")
    wait = RATE.take()
    if wait:
        _audit(request_id, "speech_busy", reason="rate_limit")
        raise refuse("speech_busy", reason="rate_limit", retry_after_s=round(wait, 1))
    try:
        body = await _read_limited(request, stt_settings.MAX_BYTES, "audio_too_large")
    except HTTPException as exc:
        _audit(request_id, exc.detail["code"])
        raise
    return await run_in_threadpool(_transcribe, site, request_id, model_id, body)


def _transcribe(site, request_id: str, model_id, body: bytes) -> dict:
    facts = {"audio_bytes": len(body), "audio_mac": _fingerprint(body)}
    try:
        answer = _run(site, request_id, model_id, body, facts)
    except HTTPException as exc:
        _audit(request_id, exc.detail["code"], **facts)
        raise
    _audit(request_id, "heard" if answer["heard"] else "not_heard", **facts)
    return answer


def _run(site, request_id: str, model_id, body: bytes, facts: dict) -> dict:
    try:
        stt_wav.parse(body)
    except stt_wav.WavError as exc:
        raise refuse(WAV_CODES[exc.code]) from None
    allowed, family = _allowed(site, model_id)
    mgr = manager()
    present = mgr.installed()
    if not present.worker:
        raise refuse("speech_not_installed")
    if not present.model:
        raise refuse("speech_model_missing")
    blocked = gate(site, _policy_status(), _loaded(mgr.status()))
    if blocked:
        reason, extra = blocked[0]
        raise refuse(GATE_CODES[reason], reasons=[r for r, _ in blocked], **extra)
    try:
        result = mgr.transcribe(
            request_id, body, site.unit.cpu_affinity, _cold_start_check(site)
        )
    except SttError as exc:
        if exc.code == "busy":
            raise refuse("speech_busy", reason="in_flight") from None
        raise refuse(STT_CODES.get(exc.code, "speech_failed")) from None
    match = None
    if result.heard:
        facts.update(
            chars=len(result.text), text_mac=_fingerprint(result.text.encode())
        )
        match = _match(result.text, allowed, family)
    return {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "request_id": request_id,
        "heard": result.heard,
        "reason": result.reason,
        "transcript": result.text,
        "language": result.language,
        "match": match,
        "engine": {
            "model": stt_settings.model_name(),
            "device": stt_settings.DEVICE,
            "loaded_now": result.loaded_now,
        },
        "timing_ms": {"load": result.load_ms, "infer": result.infer_ms},
    }
