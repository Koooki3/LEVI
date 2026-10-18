"""The real-robot side of the subtask_loop strategy: the local VLM's three
questions over its OpenAI-compatible loopback endpoint, its sleep/wake, and
the still frames they read. **Wired, not verified on the real robot or a real
vLLM** (offline tests use an injected transport; nothing here is called by
the test suite against a socket).

- ``VllmSubtaskVlm`` (``subtask_loop.ScriptedSubtaskVlm``'s real twin):
  ``capture`` takes the two camera frames from the robot's observation, keeps
  each in the run's evidence store (ref + sha256) and returns their refs;
  ``inventory``/``plan``/``check`` send the Lynx prompts
  (``subtask_loop.PROMPT_*``) with two images first, ``response_format``
  ``json_schema`` (strict), ``temperature 0`` and the per-kind ``max_tokens``.
  Nothing model-specific is in the prompts or schemas (no chat-template
  tokens); ``extra_body`` (for example a server's ``chat_template_kwargs``) is
  configuration, empty by default. The answer's text, latency and token
  counts come back as a ``subtask_loop.VlmReply``; the orchestrator validates
  it (whitelist, template), never this module.
- ``PortVlmPower``: level-1/level-2 sleep (``POST /sleep?level=n``) and
  ``POST /wake_up`` on the VLM's own loopback port, both confirmed with
  ``GET /is_sleeping`` (``levi.live.gpumgr``'s helpers); free/used GPU memory
  from ``nvidia-smi --query-gpu`` (``gpumgr.vram``, aggregate only, nobody's
  process is read); MemAvailable from ``/proc/meminfo``.

The endpoint must be an explicit numeric loopback URL that is not a robot or
policy port (``aeri.ROBOT_PORTS``).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from levi.domain import aeri

from . import camera_meta, external_report
from . import subtask_loop as st

MEMINFO = "/proc/meminfo"


def _inventory_budget(value):
    if value is None:
        return st.MAX_TOKENS["inventory"]
    if type(value) is not int or not 1 <= value <= aeri.SUBTASK_INVENTORY_TOKEN_LIMIT:
        raise ValueError("inventory_max_tokens must be a whole number from 1 to 4096")
    return value


def _extra_body(value):
    # The frozen inventory budget and fixed budgets for other questions are
    # the only output-token controls, including for direct adapter callers.
    if not isinstance(value, dict) or {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
    } & set(value):
        raise ValueError(
            "subtask_vlm.extra_body is a mapping without "
            "model/messages/max_tokens/max_completion_tokens"
        )
    return dict(value)


def loopback_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "http"
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or url.port is None
    ):
        raise ValueError("the VLM endpoint is an explicit http://127.0.0.1:<port>")
    try:
        host = ipaddress.ip_address(url.hostname)
    except ValueError:
        raise ValueError("the VLM endpoint host must be numeric loopback") from None
    if not host.is_loopback or url.port in aeri.ROBOT_PORTS:
        raise ValueError("the VLM endpoint must be loopback and not a robot port")
    return f"http://{url.hostname}:{url.port}"


def _http_post(url: str, body: dict, timeout_s: float) -> dict:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read(4 * 1024 * 1024))


class VllmSubtaskVlm:
    """``capture`` / ``inventory`` / ``plan`` / ``check`` over the VLM's
    ``/v1/chat/completions``. ``post(url, body, timeout_s)`` is the transport
    (a fake in tests); ``observe`` returns the robot's observation with
    ``images_bgr`` per view; ``encode(pixels)`` makes JPEG bytes; ``evidence``
    keeps a frame (``EvidenceStore.keep_frame``), attached once the run's
    store exists.

    Camera frame bus (CAMERA-FRAME-BUS.md): with ``image_meta`` in the
    observation each frame's JPEG is encoded once per ``(view, seq)`` and the
    frames carry ``seq``/``t_capture_ns`` into the request evidence. A
    request sends at most ``max_images`` images (default 2: the current side
    and wrist frames, as before); ``history_frames`` > 0 adds up to that many
    earlier frames per sent view from ``recent(view, n)`` (the robot's
    ``recent_frames``), within the same cap. Local and external mode share
    this code, so they follow the same rule."""

    VIEWS = ("side", "wrist")

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        observe,
        encode,
        timeout_s: float = 60.0,
        extra_body: dict | None = None,
        inventory_max_tokens: int | None = None,
        post=None,
        clock=time.monotonic,
        recent=None,
        max_images: int = camera_meta.DEFAULT_MAX_IMAGES,
        history_frames: int = 0,
    ):
        self.base_url = loopback_url(base_url)
        limits = camera_meta.image_settings(
            {"max_images": max_images, "history_frames": history_frames}
        )
        self.max_images = limits["max_images"]
        self.history_frames = limits["history_frames"]
        self.recent = recent
        if not isinstance(model, str) or not model:
            raise ValueError("the served model name is required")
        self.model = model
        self.observe = observe
        self.encode = encode
        self.timeout_s = timeout_s
        self.extra_body = _extra_body({} if extra_body is None else extra_body)
        self.inventory_max_tokens = _inventory_budget(inventory_max_tokens)
        self.post = post or _http_post
        self.clock = clock
        self.evidence = None
        self._images: dict[str, bytes] = {}
        self._jpeg = camera_meta.JpegCache(encode)
        # Earlier frames offered with a current frame's ref (history_frames).
        self._history: dict[str, list] = {}
        self._shots = 0
        self._prompts_seen: set = set()
        self._requests = 0
        self.prompt_texts: dict[str, str] = {}

    def use_evidence(self, store) -> None:
        self.evidence = store

    def capture(self, purpose: str) -> list:
        """Two still frames (side, wrist), kept as evidence; their refs (with
        ``seq``/``t_capture_ns`` when openpi gives frame metadata)."""
        obs = self.observe()
        images = obs.get("images_bgr") or {}
        meta = obs.get("image_meta")
        self._shots += 1
        out = []
        for view in self.VIEWS:
            if view not in images:
                raise ValueError(f"no {view} camera frame")
            item = (meta or {}).get(view)
            data = self._jpeg.get(view, images[view], item)
            ref = f"{view}:{purpose}-{self._shots}"
            kept = self.evidence.keep_frame(ref, data) if self.evidence else None
            if kept is None:
                kept = {"sha256": _sha256(data)}
            self._images[ref] = data
            out.append(
                {
                    "ref": ref,
                    "sha256": kept["sha256"],
                    "view": view,
                    **camera_meta.frame_fields(meta, view),
                }
            )
            if self.history_frames and self.recent is not None:
                self._history[ref] = self._earlier(view, item, ref)
        # Only the latest frames are kept in memory.
        keep = {f["ref"] for f in out}
        for ref in list(self._images):
            if ref not in keep and not ref.endswith("-start") and "-start-" not in ref:
                self._images.pop(ref, None)
        for ref in list(self._history):
            if ref not in self._images:
                self._history.pop(ref, None)
        return out

    def _earlier(self, view, item, ref) -> list:
        """Up to ``history_frames`` frames of ``view`` older than the current
        one (oldest first), JPEG-encoded once each; kept as evidence only
        when a request sends them."""
        try:
            frames = self.recent(view, self.history_frames + 1) or []
        except Exception:  # noqa: BLE001 - no history: the current frames only
            return []
        current = item.get("seq") if isinstance(item, dict) else None
        older = [
            f
            for f in frames
            if isinstance(f, dict)
            and type(f.get("seq")) is int
            and f.get("image") is not None
            and (current is None or f["seq"] < current)
        ]
        if current is None:
            older = older[:-1]  # the newest is (about) the current frame
        out = []
        for frame in older[-self.history_frames :]:
            out.append(
                {
                    "ref": f"{ref}-h{frame['seq']}",
                    "view": view,
                    "seq": frame["seq"],
                    "t_capture_ns": frame.get("t_capture_ns"),
                    "data": self._jpeg.get(view, frame["image"], frame),
                }
            )
        return out

    def _selected(self, images: list) -> list:
        """The frames one request sends: the given ones first, then their
        earlier frames, never more than ``max_images``."""
        send = [dict(f) for f in images[: self.max_images]]
        if self.history_frames:
            for f in list(send):
                for extra in self._history.get(f["ref"], []):
                    if len(send) >= self.max_images:
                        break
                    data = extra["data"]
                    kept = (
                        self.evidence.keep_frame(extra["ref"], data)
                        if self.evidence
                        else None
                    )
                    self._images[extra["ref"]] = data
                    send.append(
                        {
                            "ref": extra["ref"],
                            "sha256": (kept or {}).get("sha256") or _sha256(data),
                            "view": extra["view"],
                            "seq": extra["seq"],
                            "t_capture_ns": extra["t_capture_ns"],
                            "earlier": True,
                        }
                    )
        return send

    def _ask(self, kind: str, text: str, images: list, schema: dict) -> st.VlmReply:
        given = len(images)
        images = self._selected(images)
        if len(images) > min(given, self.max_images):
            text = (
                text
                + f"\nImages {min(given, self.max_images) + 1} to {len(images)} are "
                "earlier frames of the same cameras, oldest first."
            )
        content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64,"
                    + base64.b64encode(self._images[f["ref"]]).decode()
                },
            }
            for f in images
        ]
        content.append({"type": "text", "text": text})
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": (
                self.inventory_max_tokens
                if kind == "inventory"
                else st.MAX_TOKENS[kind]
            ),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": kind, "strict": True, "schema": schema},
            },
            **self.extra_body,
        }
        digest = st.prompt_sha256(text)
        if digest not in self._prompts_seen:
            self._prompts_seen.add(digest)
            self.prompt_texts[digest] = text
        self._requests += 1
        prompt = external_report.text_snapshot(text)
        common = {
            "prompt_sha256": digest,
            "prompt_text": prompt["text"],
            "prompt_bytes": prompt["bytes"],
            "prompt_text_truncated": prompt["truncated"],
            "client_request_id": f"{kind}-{self._requests}",
            "image_refs": [f["ref"] for f in images],
            # Which camera frame each sent image is (frame bus; absent: None).
            "image_meta": [
                {
                    "ref": f["ref"],
                    "view": f.get("view"),
                    "seq": f["seq"],
                    "t_capture_ns": f.get("t_capture_ns"),
                }
                for f in images
                if "seq" in f
            ]
            or None,
            "timeout_s": self.timeout_s,
        }
        began = self.clock()
        try:
            found = self.post(
                self.base_url + "/v1/chat/completions", body, self.timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - a timeout or HTTP error: no answer
            return st.VlmReply(
                raw=None,
                latency_ms=int((self.clock() - began) * 1000),
                response_schema_ok=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
                **common,
            )
        latency = int((self.clock() - began) * 1000)
        raw, error, finish_reason = None, None, None
        try:
            choice = found["choices"][0]
            finish_reason = (
                choice.get("finish_reason") if isinstance(choice, dict) else None
            )
            if not isinstance(finish_reason, str):
                finish_reason = None
            raw = choice["message"]["content"]
            if not isinstance(raw, str):
                raw, error = None, "content is not text"
        except (KeyError, IndexError, TypeError) as exc:
            error = f"no choices[0].message.content ({type(exc).__name__})"
        if finish_reason == "length":
            error = "response truncated: finish_reason=length"
        usage = (found.get("usage") or {}) if isinstance(found, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        # Missing/invalid counts are unknown, never an inferred zero. bool is
        # an int subclass, but is not a token measurement.
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if type(prompt_tokens) is not int or prompt_tokens < 0:
            prompt_tokens = None
        if type(completion_tokens) is not int or completion_tokens < 0:
            completion_tokens = None
        return st.VlmReply(
            raw=raw,
            external_evidence=external_report.verify_evidence(
                found.get("levi_external"), found.get("id"), raw
            )
            if isinstance(found, dict) and error is None
            else None,
            finish_reason=finish_reason,
            latency_ms=latency,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_source=(
                "provider_reported"
                if prompt_tokens is not None or completion_tokens is not None
                else "unavailable"
            ),
            request_id=found.get("id") if isinstance(found, dict) else None,
            response_schema_ok=error is None,
            error=error,
            **common,
        )

    def inventory(self, *, frames):
        text = st.prompt_text("inventory")
        return self._ask("inventory", text, frames, st.SCHEMA_INVENTORY)

    def plan(self, *, loop, task, whitelist, frames, reference=None, round_no=None):
        text = st.prompt_text(
            "plan", task=task, whitelist=list(whitelist), direction=loop
        )
        if loop == "reverse":
            # Lynx: the side camera now, then the side camera at the round start.
            side = [f for f in frames if f["view"] == "side"]
            start = [f for f in reference or [] if f["view"] == "side"]
            images = side[:1] + start[:1]
        else:
            images = list(frames)
        return self._ask("plan", text, images, st.SCHEMA_PLAN)

    def check(self, *, subtask, frames):
        text = st.prompt_text("check", facts=subtask.facts_text())
        return self._ask("check", text, frames, st.SCHEMA_CHECK)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def mem_available_gib(path=MEMINFO) -> float | None:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


class PortVlmPower(st.VlmPower):
    """Sleep/wake of the VLM on its own loopback port, confirmed by
    ``/is_sleeping``; readings from ``nvidia-smi`` (aggregate) and
    ``/proc/meminfo``. ``post``/``sleeping``/``vram``/``meminfo`` are
    injectable (tests)."""

    def __init__(
        self,
        port: int,
        *,
        wake_timeout_s: float = 60.0,
        sleep_timeout_s: float = 60.0,
        gpu_index: int = 0,
        post=None,
        sleeping=None,
        vram=None,
        meminfo=MEMINFO,
        gpu_memory_utilization=None,
        verify,
    ):
        """``verify()``: the listener on ``port`` is our own recorded VLM unit
        (``subtask_units.Services.owns_vlm``: pid and starttime as recorded,
        R15). Asked before every ``/sleep`` and ``/wake_up``; anything but
        ``True`` sends nothing (``last_refusal``)."""
        from levi.live import gpumgr

        if not callable(verify):
            raise TypeError("PortVlmPower needs the ownership check (verify)")
        self.verify = verify
        self.last_refusal = None

        if (
            type(port) is not int
            or port in aeri.ROBOT_PORTS
            or not 1024 <= port <= 65535
        ):
            raise ValueError(
                "the VLM port must be a loopback port that is not a robot's"
            )
        self.port = port
        self.wake_timeout_s = wake_timeout_s
        self.sleep_timeout_s = sleep_timeout_s
        self.gpu_index = gpu_index
        self._post = post or gpumgr._post
        self._sleeping = sleeping or gpumgr.is_sleeping
        self._vram = vram or (lambda: gpumgr.vram(self.gpu_index))
        self.meminfo = meminfo
        self.gpu_memory_utilization = gpu_memory_utilization

    def wake_need_mib(self):
        """``gpumgr.need_mib(wake=True)`` for this profile: the budget of
        vLLM's total less what a level-1-asleep vLLM still holds, plus the live
        service's default wake margin (None without a profile or a reading)."""
        from levi.live import gpumgr
        from levi.live.config import Gpu

        util = self.gpu_memory_utilization
        found = self._vram()
        if not util or not found:
            return None
        budget = util * (found["total_mib"] - gpumgr.VLLM_TOTAL_SLACK_MIB)
        return max(0, int(budget) + Gpu().wake_margin_mib - gpumgr.ASLEEP_RESIDENT_MIB)

    def _ours(self) -> bool:
        """R15 / m28: the one ownership gate in front of every power call."""
        try:
            ok = self.verify() is True
        except Exception:  # noqa: BLE001 - an unreadable owner is not ours
            ok = False
        self.last_refusal = None if ok else "not_our_unit"
        return ok

    def wake(self) -> bool:
        if not self._ours():
            return False
        return (
            self._post(self.port, "/wake_up", timeout=self.wake_timeout_s) is True
            and self._sleeping(self.port) is False
        )

    def sleep(self, level: int = 1) -> bool:
        if level not in (1, 2):
            return False
        if not self._ours():
            return False
        return (
            self._post(self.port, f"/sleep?level={level}", timeout=self.sleep_timeout_s)
            is True
            and self._sleeping(self.port) is True
        )

    def free_gpu_mib(self):
        found = self._vram()
        return None if not found else found.get("free_mib")

    def gpu_totals(self):
        found = self._vram()
        if not found:
            raise ValueError("nvidia-smi unreadable")
        return found["used_mib"], found["total_mib"]

    def mem_available_gib(self):
        return mem_available_gib(self.meminfo)


# --- what a subtask_loop run does not use (it has no scene check, no goal
# judge and no event stream: the VLM sleeps while the policy runs) ------------


class NoEvents:
    def open(self, episode_id, role):
        return episode_id

    def poll(self, stream_id, *, through_step, max_events):
        return []

    def close(self, stream_id):
        return None


class NoJudge:
    """The media writer's judge buffer, kept nowhere."""

    class _Buffer:
        def add(self, *args):
            return None

    def new_buffer(self, hz):
        return self._Buffer()


class Refused:
    """A scene provider or goal verifier this mode never asks; asked anyway,
    it refuses (so nothing could ever release motion through it)."""

    def submit(self, request):
        raise RuntimeError("subtask_loop has no scene check or goal judge")

    def collect(self, ticket, *, timeout_ns):
        return None

    def cancel(self, ticket, reason):
        return None


def binding(value, job) -> dict:
    """The binding's ``subtask_vlm`` block: ``base_url`` (loopback, the job's
    ``subtask.vlm.port``), ``model`` (the served name), optional
    ``extra_body`` (a mapping sent with every request), ``gpu_index`` and
    ``service`` (terminal D starts and stops the VLM itself,
    ``subtask_units``: ``unit``, ``argv``, optional ``env``, ``gpu_lock``,
    ``need_policy_mib``, ``ready_timeout_s``)."""
    if not isinstance(value, dict) or not {"base_url", "model"} <= set(value) <= {
        "base_url",
        "model",
        "extra_body",
        "gpu_index",
        "service",
        "max_images",
        "history_frames",
    }:
        raise ValueError(
            "subtask_vlm requires base_url and model "
            "(optional extra_body, gpu_index, service, max_images, history_frames)"
        )
    images = camera_meta.image_settings(value)
    url = loopback_url(value["base_url"])
    port = int(url.rsplit(":", 1)[1])
    planned = (job["plan"].get("subtask") or {}).get("vlm") or {}
    if planned.get("port") != port:
        raise ValueError(
            "subtask_vlm.base_url port differs from the job's subtask.vlm.port"
        )
    served = planned.get("served_name")
    if served is not None and value["model"] != served:
        raise ValueError(
            "subtask_vlm.model differs from the job's subtask.vlm.served_name"
        )
    extra = _extra_body(value.get("extra_body", {}))
    inventory_budget = _inventory_budget(planned.get("inventory_max_tokens"))
    if planned.get(
        "inventory_max_tokens"
    ) is not None and inventory_budget >= planned.get(
        "min_model_len", planned.get("max_model_len", 16384)
    ):
        raise ValueError("subtask.vlm.inventory_max_tokens must be below min_model_len")
    index = value.get("gpu_index", 0)
    if type(index) is not int or index < 0:
        raise ValueError("subtask_vlm.gpu_index is a whole number >= 0")
    if "service" not in value:
        raise ValueError(
            "subtask_vlm.service is required: terminal D starts the VLM as its own "
            "unit, and sleep/wake are sent only to that recorded unit (R15)"
        )
    service = _service(value["service"], port, planned)
    return {
        "service": service,
        "base_url": url,
        "port": port,
        "model": value["model"],
        "extra_body": extra,
        "inventory_max_tokens": inventory_budget,
        "gpu_index": index,
        "max_images": images["max_images"],
        "history_frames": images["history_frames"],
        "timeout_s": planned.get("request_timeout_s", 60),
        "gpu_memory_utilization": planned.get("gpu_memory_utilization"),
        "wake_timeout_s": planned.get("wake_timeout_s", 60),
    }


def _flag(argv, name):
    """The value after ``name`` in ``argv`` (None: absent or last)."""
    if name not in argv or argv.index(name) + 1 >= len(argv):
        return None
    return argv[argv.index(name) + 1]


def _service(value, port, planned=None) -> dict:
    """The reviewed launch of terminal D's own VLM unit (no shell: an
    argument with whitespace, ``$`` or ``%`` is refused, so systemd expands
    nothing; the argv must serve on the job's port)."""
    import re

    from .subtask_units import VLM_UNIT

    keys = {"unit", "argv", "env", "gpu_lock", "need_policy_mib", "ready_timeout_s"}
    if (
        not isinstance(value, dict)
        or not {"unit", "argv", "gpu_lock", "need_policy_mib"} <= set(value) <= keys
    ):
        raise ValueError(
            "subtask_vlm.service requires unit, argv, gpu_lock and need_policy_mib "
            "(optional env, ready_timeout_s)"
        )
    if not isinstance(value["unit"], str) or not VLM_UNIT.fullmatch(value["unit"]):
        raise ValueError("subtask_vlm.service.unit must be a levi-aeri-vlm-* name")
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(a, str) or not a or re.search(r"[\s$%]", a) for a in argv)
        or not Path(argv[0]).is_absolute()
    ):
        raise ValueError(
            "subtask_vlm.service.argv is an absolute program and plain arguments"
        )
    if (
        "--port" not in argv
        or argv.index("--port") + 1 >= len(argv)
        or argv[argv.index("--port") + 1] != str(port)
    ):
        raise ValueError("subtask_vlm.service.argv must serve on the job's VLM port")
    check_argv(argv, port, planned or {})
    env = value.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(k, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", k)
        or not isinstance(v, str)
        or re.search(r"[\s$%]", v)
        for k, v in env.items()
    ):
        raise ValueError("subtask_vlm.service.env maps NAMES to plain values")
    lock = value["gpu_lock"]
    if not isinstance(lock, str) or not Path(lock).is_absolute():
        raise ValueError(
            "subtask_vlm.service.gpu_lock is LEVI's absolute .gpu.lock path"
        )
    need = value["need_policy_mib"]
    if type(need) is not int or need < 1:
        raise ValueError("subtask_vlm.service.need_policy_mib is a whole number >= 1")
    ready = value.get("ready_timeout_s", 1800)
    if type(ready) not in (int, float) or not 0 < ready <= 7200:
        raise ValueError("subtask_vlm.service.ready_timeout_s is 0 < s <= 7200")
    return {
        "unit": value["unit"],
        "argv": list(argv),
        "env": dict(env),
        "gpu_lock": lock,
        "need_policy_mib": need,
        "ready_timeout_s": ready,
    }


def check_argv(argv, port, planned) -> None:
    """m29: the unit's launch must be the job's frozen VLM configuration:
    the model, the port, max_model_len and gpu_memory_utilization (and the
    served name when the job names one). A mismatch refuses the run before
    anything starts."""
    model = planned.get("model")
    if not model:
        raise ValueError(
            "subtask.vlm.model is required when terminal D starts the VLM "
            "(the launch is checked against it)"
        )
    problems = []
    if model not in argv[1:]:
        problems.append(f"model {model!r} is not in the argv")
    if _flag(argv, "--port") != str(port):
        problems.append(f"--port is not {port}")
    want = planned.get("max_model_len")
    if _flag(argv, "--max-model-len") != str(want):
        problems.append(f"--max-model-len is not {want}")
    util = planned.get("gpu_memory_utilization")
    found = _flag(argv, "--gpu-memory-utilization")
    try:
        same = found is not None and abs(float(found) - float(util)) < 1e-9
    except (TypeError, ValueError):
        same = False
    if not same:
        problems.append(f"--gpu-memory-utilization is not {util}")
    served = planned.get("served_name")
    if served is not None and _flag(argv, "--served-model-name") != served:
        problems.append(f"--served-model-name is not {served}")
    if problems:
        raise ValueError(
            "subtask_vlm.service.argv differs from the job's subtask.vlm: "
            + "; ".join(problems)
        )
