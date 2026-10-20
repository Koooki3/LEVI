"""Regenerates ``src/components/marvel0/__tests__/marvel0-deploy.samples.json``: the
exact answers of the marvel0's policy-deploy and catalog routes on a fully fake marvel0,
which the frontend tests (``marvel0-deploy-samples.test.ts``) parse with the page's
own readers, so the page and the server agree on one shape.

    uv run --no-sync python tests/marvel0/regen_deploy_sample.py

Every answer is built in process: the fake machine of ``test_marvel0_deploy.py``
(temporary folders, a fake ``/proc``, scripted ``systemctl``/``systemd-run``/
``nvidia-smi``, a clock moved by hand) behind a ``Supervisor``, and the catalog
router on a test client with a temporary site file. No process is started, no
socket opened, nothing of the real machine looked at. The temporary folder is
written ``/path/to`` in the file, as the site file template does.
``tests/marvel0/test_marvel0_deploy_sample.py`` fails, naming this command, when the
committed file differs from what this builds."""

import contextlib
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from regen_sample import FOLDER, ROOT, fixed_zone
from site_sample import make_checkpoints, write_site
from test_marvel0_deploy import CFG, STOP, TOTAL_MIB, Marvel0

from levi.marvel0 import catalog_api, models
from levi.marvel0 import site as marvel0_site

SAMPLE = FOLDER / "marvel0-deploy.samples.json"
PROMPT = "pick the eggplant in the blue plate"
MODEL = "pi05_fr3_all_step49999"
OOM_LINE = (
    "jaxlib.xla_extension.XlaRuntimeError: RESOURCE_EXHAUSTED: "
    "Out of memory while trying to allocate 123 bytes"
)


@contextlib.contextmanager
def marvel0():
    """A fresh fake machine (and the patches it makes, undone afterwards)."""
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as patch:
        yield Marvel0(Path(tmp), patch)


def log_of(r) -> dict:
    """The log answer with the size the file has when the fake machine's folder
    is read as the template path. The real size counts the temporary folder's
    name (a log line names the checkpoint folder), and that name carries the
    checkout's path, so it would differ between checkouts."""
    answer = r.sup.log(50)
    shown = r.log_path().read_text()
    for folder in sorted({str(r.tmp), str(r.tmp.resolve())}, key=len, reverse=True):
        shown = shown.replace(folder, "/path/to")
    answer["size_bytes"] = len(shown.encode())
    return answer


def view(r):
    r.sup.invalidate()
    return r.sup.status()


def came_up(r, **kw):
    answer = r.deploy(**kw)
    r.server_comes_up(**{k: v for k, v in kw.items() if k in ("rtc", "cfg")})
    return answer


def refusal(call, *args, **kw):
    """``{"status", "detail"}`` of the refusal a call raises."""
    with pytest.raises(Exception) as caught:
        call(*args, **kw)
    return {"status": caught.value.status_code, "detail": caught.value.detail}


def deploy_samples() -> dict:
    out = {}
    with marvel0() as r:
        out["idle"] = view(r)
    with marvel0() as r:
        r.hold(r.site.arm_lock_file, 5001)
        out["blocked_arm"] = view(r)
    with marvel0() as r:
        out["deployed"] = r.deploy()
        out["starting"] = view(r)
        out["log_empty"] = r.sup.log(50)
    with marvel0() as r:
        came_up(r)
        view(r)  # the first look that sees it ready
        r.clock.advance(10)
        out["ready"] = view(r)
        out["log_ready"] = log_of(r)
        r.connect()
        out["ready_with_client"] = view(r)
        r.disconnect()
        out["stopped"] = r.sup.stop({**STOP, "request_id": "stop-request-1"})
        out["idle_after_stop"] = view(r)
    with marvel0() as r:
        r.deploy(model=CFG, accept_unverified=True)
        r.server_comes_up(model=CFG, cfg=True)
        r.clock.advance(10)
        out["ready_cfg"] = view(r)
    with marvel0() as r:
        r.deploy()
        r.log("INFO:root:Loading model...", OOM_LINE)
        r.units.clear()
        out["failed_oom"] = view(r)
        out["log_oom"] = log_of(r)
    with marvel0() as r:
        r.listen()
        out["foreign"] = view(r)
        out["port_taken"] = refusal(r.deploy)
    return out


def refusal_samples() -> dict:
    out = {}
    with marvel0() as r:
        r.gpu = [TOTAL_MIB - 5100, TOTAL_MIB]
        out["gpu_insufficient"] = refusal(r.deploy, fraction=0.3)
    with marvel0() as r:
        out["unverified_not_accepted"] = refusal(r.deploy, model=CFG)
    with marvel0() as r:
        r.deploy(model=CFG, accept_unverified=True)
        out["already_running"] = refusal(
            r.deploy, model=CFG, accept_unverified=True, request_id="req-000002-bbbb"
        )
    return out


def catalog_samples(tmp: Path) -> dict:
    """The catalog router on a test client: models, site, previews, and the
    answers for a missing site file and a malformed body."""
    make_checkpoints(tmp)
    app = FastAPI()
    app.include_router(catalog_api.router)
    client = TestClient(app)
    out = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LEVI_MARVEL0_SITE", str(write_site(tmp)))
        marvel0_site.clear_cache()
        models.cache_clear()
        base = {"model_id": MODEL, "prompt": PROMPT}
        out["models"] = client.get("/api/levi/marvel0/models").json()
        out["site"] = client.get("/api/levi/marvel0/site").json()
        preview = "/api/levi/marvel0/command/preview"
        out["preview"] = client.post(preview, json=base).json()
        out["preview_warn"] = client.post(
            preview,
            json={
                "model_id": "recap_cfg_r2_best_step14300_jax",
                "prompt": PROMPT,
                "min_z": 0.03,
                "rtc_horizon": 8,
            },
        ).json()
        for name, extra in (
            ("preview_min_z", {"min_z": 0.01}),
            ("preview_type", {"max_steps": "5"}),
        ):
            reply = client.post(preview, json={**base, **extra})
            out[name] = {"status": reply.status_code, "body": reply.json()}
        patch.setenv("LEVI_MARVEL0_SITE", str(tmp / "none.toml"))
        marvel0_site.clear_cache()
        reply = client.get("/api/levi/marvel0/site")
        out["site_missing"] = {"status": reply.status_code, "body": reply.json()}
    marvel0_site.clear_cache()
    models.cache_clear()
    return out


WIDTH = 80  # prettier's print width


def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _lines(value, indent, head, tail) -> list:
    """``head`` + the value + ``tail`` as lines the way prettier prints JSON, so
    that ``format:check`` of the frontend accepts the file: an object is always
    open, keys sorted; an array of plain values is on one line when it fits in
    80 columns, else one value per line."""
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
            if len(one) <= WIDTH:
                return [one]
        out = [f"{pad}{head}["]
        for n, item in enumerate(value):
            out += _lines(item, indent + 2, "", "," if n < len(value) - 1 else "")
        return [*out, f"{pad}]{tail}"]
    return [f"{pad}{head}{_scalar(value)}{tail}"]


def build() -> str:
    """The file's text. The deploy answers come from one fake machine each; the
    refusals the page words with their facts are collected under ``refusals``."""
    with tempfile.TemporaryDirectory() as tmp, fixed_zone():
        samples = deploy_samples()
        samples["refusals"] = {
            "port_taken": samples.pop("port_taken"),
            **refusal_samples(),
        }
        samples.update(catalog_samples(Path(tmp)))
        # (the answers above are the supervisor's own; the deploy router passes
        # them through unchanged.)
        text = "\n".join(_lines(samples, 0, "", ""))
        # Paths of the fake machines read as the template's: a file that names
        # the temporary folder would differ at every build.
        text = text.replace(str(Path(tmp)), "/path/to")
    return text + "\n"


if __name__ == "__main__":
    SAMPLE.write_text(build())
    print(f"wrote {SAMPLE.relative_to(ROOT)}")
