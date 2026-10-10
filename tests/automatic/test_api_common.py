"""Shared fixtures of the HTTP interface tests (T-API-1). No test lives here.

``LEVI_AERI_HOME`` and the job roots always point into ``tmp_path``; the
policy root is a made-up folder; a launch runs the dry run in the calling
thread (``api.LAUNCH_BACKEND = "inprocess"``), so no unit is ever started.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_aeri_cli import CONTRACT, job_text

from levi.automatic import api, launch

REQ = {"x-levi-ui-token": "test-token"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "aeri-home"
    monkeypatch.setenv(launch.HOME_ENV, str(folder))
    return folder


@pytest.fixture
def roots(tmp_path, home, monkeypatch):
    folder = tmp_path / "jobs"
    folder.mkdir()
    monkeypatch.setenv(launch.JOB_ROOTS_ENV, str(folder))
    return folder


@pytest.fixture
def client(roots, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-token")
    monkeypatch.delenv(api.POLICY_ROOT_ENV, raising=False)
    monkeypatch.setattr(api, "LAUNCH_BACKEND", "inprocess")
    monkeypatch.setattr(api, "LAUNCH_DEADLINE_S", 5.0)
    api._POLICY_CACHE.update(key=None, at=0.0, report=None)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, headers=REQ) as test_client:
        yield test_client


def write_job(
    roots: Path, tmp_path: Path, name="r-api", strategy="single_reset_policy", **over
):
    """A job file (and its contract) in the job root; returns its path."""
    rollouts = tmp_path / "rollouts"
    rollouts.mkdir(exist_ok=True)
    (roots / "initial-state.yaml").write_text(CONTRACT)
    path = roots / f"{name}.yaml"
    path.write_text(
        job_text(
            rollouts,
            name=name,
            strategy=strategy,
            forward_folder=f"stack__{name}",
            reset_folder=f"reset_stack__{name}",
            **over,
        )
    )
    return path


def job_id_of(client, name: str) -> str:
    rows = client.get("/api/levi/automatic/jobs").json()["jobs"]
    return next(r["id"] for r in rows if r["name"] == f"{name}.yaml")


def plan_and_launch(client, job_id, request_id="req-launch-1", **plan_body):
    """Plan a dry run and launch it; returns (plan, response)."""
    plan = client.post(
        "/api/levi/automatic/plan",
        json={"job_id": job_id, "execution_mode": "dry_run", **plan_body},
    ).json()
    response = client.post(
        "/api/levi/automatic/runs",
        json={
            "job_id": job_id,
            "plan_sha256": plan["plan_sha256"],
            "launch_token": plan["launch_token"],
            "confirm": "launch",
            "request_id": request_id,
        },
    )
    return plan, response


def background_launch(client, job_id, request_id="req-bg-1", **plan_body):
    """Launch in a thread (the in-process runner serves until its deadline);
    returns (thread, results list)."""
    out = []

    def go():
        out.append(plan_and_launch(client, job_id, request_id, **plan_body))

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, out


def wait_for(predicate, timeout=20.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(step)
    raise AssertionError("timed out waiting")


def run_dirs(home: Path) -> list:
    folder = home / launch.RUNS
    return sorted(json.loads(p.read_text())["run_dir"] for p in folder.glob("*.json"))


__all__ = [
    "CONTRACT",
    "REQ",
    "background_launch",
    "client",
    "home",
    "job_id_of",
    "os",
    "plan_and_launch",
    "roots",
    "run_dirs",
    "wait_for",
    "write_job",
]
