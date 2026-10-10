"""Shared fixtures of the campaign HTTP and controller tests (T-API-2). No
test lives here.

Everything points into ``tmp_path``: ``LEVI_AERI_HOME`` (the campaign
guard's fixture), the job roots, the policy root (made-up checkpoint
folders), the operator guide and the legacy client's rollout root. The
controller runs in a thread of the test (``CONTROLLER_BACKEND =
"inprocess"``) and so do the dry runs of a dry-run campaign; the systemd
tool is never run (the campaign guard refuses it) and the one function that
would start a unit, ``controller.start_unit``, is replaced where a test
looks at it. No socket is opened: the guard records any attempt and fails
the test when it is a robot or policy port.
"""

import json
import time
from pathlib import Path

import pytest
from campaign_guard import aeri_home_fixture, guard_fixture
from fastapi import FastAPI
from fastapi.testclient import TestClient

from levi.automatic import api as base
from levi.automatic import jobs_wizard
from levi.automatic.campaign import adapters as A
from levi.automatic.campaign import api as capi
from levi.automatic.campaign import controller as C

URL = "/api/levi/automatic/campaigns"
REQ = {"x-levi-ui-token": "test-token"}
CKPTS = ("pi05_a", "recap_b", "recap_c")

# A guide shaped like setup.md §6.3 with made-up machine values.
GUIDE = """# Operator guide

### 6.3 Dual-label evaluation

```bash
uv run examples/client.py \\
  --remote-host=127.0.0.1 --remote-port=8000 \\
  --view-camera-id 111111111111 --wrist-camera-id 222222222222 \\
  --prompt "pick the eggplant in the blue plate" \\
  --eval-num 50 --min-z 0.05 \\
  --levi-mode dual --new --no-display-images \\
  --rollout-group pi05_fr3_all_step49999 \\
  --eval-note "pi05_fr3_all_step49999 formal"
```
"""


def make_checkpoint(root, name, *, config="pi05_fr3_all_state", role="forward"):
    folder = Path(root) / name
    (folder / "params").mkdir(parents=True)
    (folder / "assets").mkdir()
    (folder / "norm_stats.json").write_text("{}")
    (folder / "VERSION.json").write_text(
        json.dumps({"config": config, "policy_role": role})
    )
    return folder


class World:
    def __init__(self, tmp_path, client):
        self.tmp = tmp_path
        self.client = client
        self.jobs = tmp_path / "jobs"
        self.rollouts = tmp_path / "rollouts"

    def make_job(self, name="base", strategy="single_reset_policy", **over):
        """A wizard-shaped job file in the job root; returns its job id."""
        form = {
            "request_id": f"req-{name}",
            "name": name,
            "task": {"instruction": "stack the plates"},
            "policy_forward": {"checkpoint_id": "pi05_a"},
            "reset": {"strategy": strategy, "scene_check": "provider"},
            "run": {"episodes": 2, "max_steps": 40},
        }
        reset = None
        if strategy == "single_reset_policy":
            form["policy_reset"] = {"checkpoint_id": "pi05_a"}
            reset = {"id": "pi05_a", "config": "c"}
        form.update(over)
        parsed = jobs_wizard.parse_form(form)
        text = jobs_wizard.build_text(parsed, {"id": "pi05_a", "config": "c"}, reset)
        path = self.jobs / f"{name}.yaml"
        path.write_text(text)
        return base.job_id_of(str(self.jobs.resolve()), f"{name}.yaml")

    def plan_body(self, job_id, **over):
        body = {
            "job_id": job_id,
            "arms": [
                {"id": "A", "checkpoint_id": "pi05_a", "role": "reference"},
                {"id": "B", "checkpoint_id": "recap_b", "role": "candidate"},
            ],
            "trials_per_arm": 4,
            "schedule": {
                "kind": "counterbalanced_segments",
                "segment_trials": 2,
                "seed": 7,
            },
            "primary": {
                "metric": "success",
                "label_basis": "autonomous_verdict",
                "alpha": 0.05,
            },
            "preregistered": False,
        }
        body.update(over)
        return body

    def plan(self, job_id, **over):
        return self.client.post(f"{URL}/plan", json=self.plan_body(job_id, **over))

    def start(self, job_id, request_id="req-start-1", **over):
        body = self.plan_body(job_id, **over)
        plan = self.client.post(f"{URL}/plan", json=body)
        assert plan.status_code == 200, plan.text
        body.update(
            plan_sha256=plan.json()["plan_sha256"],
            confirm="start-campaign",
            request_id=request_id,
        )
        return self.client.post(URL, json=body), plan.json()

    def dump(self, campaign_id):
        """The journal's last lines and the ``ctl/`` files (failure text)."""
        folder = Path(self.tmp) / "aeri-home" / "campaigns" / campaign_id
        lines = []
        try:
            for line in (folder / "journal.jsonl").read_text().splitlines()[-12:]:
                j = json.loads(line)
                lines.append(
                    (
                        j["sequence_no"],
                        j["record"],
                        j.get("to_state"),
                        j.get("reason"),
                        j.get("note"),
                    )
                )
        except OSError:
            pass
        ctl = {
            p.name: sorted(q.name for q in p.iterdir())
            for p in (folder / "ctl").iterdir()
            if p.is_dir()
        }
        return f"journal={lines} ctl={ctl}"

    def snapshot(self, campaign_id):
        answer = self.client.get(f"{URL}/{campaign_id}")
        assert answer.status_code == 200, (answer.status_code, answer.text)
        return answer.json()

    def wait_state(self, campaign_id, states, timeout=30.0):
        wanted = (states,) if isinstance(states, str) else tuple(states)
        return wait_for(
            lambda: (s := self.snapshot(campaign_id))["state"] in wanted and s,
            timeout,
            describe=lambda: self.dump(campaign_id),
        )

    def wait_todo(self, campaign_id, kind, timeout=30.0):
        return wait_for(
            lambda: (
                (s := self.snapshot(campaign_id))["todo"]
                and s["todo"]["kind"] == kind
                and s
            ),
            timeout,
        )

    def confirm(self, campaign_id, todo, command_id, kind=None, **extra):
        kind = kind or {
            "switch_policy": "switch_policy",
            "segment_done": "segment_done",
        }.get(todo["kind"], "env")
        return self.client.post(
            f"{URL}/{campaign_id}/confirm",
            json={
                "command_id": command_id,
                "kind": kind,
                "challenge": todo["challenge"],
                **extra,
            },
        )

    def command(self, campaign_id, action, command_id):
        return self.client.post(
            f"{URL}/{campaign_id}/{action}",
            json={"command_id": command_id, "confirm": action},
        )


def write_rollout(
    root,
    group,
    number,
    *,
    note,
    run_id,
    operator="success",
    agent="success",
    ended_by="budget",
    task="pick",
):
    """One legacy-client rollout (``metadata.json`` and ``.complete``)."""
    path = Path(root) / group / task / f"demo_{number:04d}"
    path.mkdir(parents=True)
    created = f"2026-10-10T10:{number % 60:02d}:00+0800"
    meta = {
        "created_at": created,
        "stopped_at": created,
        "eval": {
            "episode_in_session": number,
            "eval_note": note,
            "max_steps": 400,
            "run_id": run_id,
            "outcome": operator,
            "verdict_by": "operator",
            "counted": True,
            "steps": 400,
            "ended_by": ended_by,
            "operator_outcome": operator,
            "label_mode": "dual",
            "agent_label": {
                "source": "online",
                "status": "ok",
                "outcome": agent,
                "undecided": False,
                "spec": {"id": "generic-final", "version": 1},
                "timing": "after_budget",
            },
        },
    }
    (path / "metadata.json").write_text(json.dumps(meta))
    (path / ".complete").write_text("")
    return path


def wait_for(predicate, timeout=30.0, step=0.05, describe=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(step)
    detail = describe() if describe else ""
    raise AssertionError(f"timed out waiting {detail}")


@pytest.fixture
def world(tmp_path, aeri_home, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    policies = tmp_path / "ckpt"
    policies.mkdir()
    for name in CKPTS:
        make_checkpoint(policies, name)
    guide = tmp_path / "guide.md"
    guide.write_text(GUIDE)
    rollouts = tmp_path / "rollouts"
    rollouts.mkdir()
    monkeypatch.setenv("LEVI_AERI_JOB_ROOTS", str(jobs))
    monkeypatch.setenv(base.POLICY_ROOT_ENV, str(policies))
    monkeypatch.setenv("LEVI_SETUP_DOC", str(guide))
    monkeypatch.setenv(C.ROLLOUT_ROOT_ENV, str(rollouts))
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-token")
    monkeypatch.setattr(C, "CONTROLLER_BACKEND", "inprocess")
    monkeypatch.setattr(C, "CHILD_BACKEND", "inprocess")
    monkeypatch.setattr(C, "CHILD_DEADLINE_S", 30.0)
    monkeypatch.setattr(C, "REPORT_RESAMPLES", 200)
    monkeypatch.setattr(C, "REPORT_PDF", False)
    base._POLICY_CACHE.update(key=None, at=0.0, report=None)
    base._OPERATIONS.clear()
    capi._MEMO.items.clear()
    capi._COUNTS.clear()
    capi._CHALLENGES.by_run.clear()
    app = FastAPI()
    app.include_router(base.router)
    app.include_router(capi.router)
    with TestClient(app, headers=REQ) as client:
        yield World(tmp_path, client)
    C.stop_all()


__all__ = [
    "REQ",
    "URL",
    "A",
    "World",
    "aeri_home_fixture",
    "guard_fixture",
    "make_checkpoint",
    "wait_for",
    "world",
    "write_rollout",
]
