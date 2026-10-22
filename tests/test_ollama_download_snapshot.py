"""Deterministic read/finish interleaving; SQLite only, no service connection."""

import sqlite3
import threading
import time

import pytest
from conftest import scaled

from levi.agent.schema import ProviderConfig
from levi.agent.store import Store, close_thread_connections
from levi.inference import models


@pytest.mark.parametrize("finished", ["cancelled", "succeeded", "failed"])
def test_download_read_keeps_one_snapshot_when_worker_finishes(
    tmp_path, monkeypatch, finished
):
    store = Store(tmp_path)
    config = ProviderConfig(
        name="ollama",
        kind="ollama",
        base_url="http://127.0.0.1:11434",
        model="fixture",
        allow_localhost=True,
    )
    job_id = "model-snapshot"
    lease = models._lease(config)
    store.put(
        "model_downloads",
        job_id,
        {
            "id": job_id,
            "status": "running",
            "config": config.model_dump(),
            "cancel_requested": finished == "cancelled",
        },
    )
    assert store.claim(lease, job_id, ttl=90)
    read = threading.Event()
    done = threading.Event()
    errors = []
    original_get = store.get
    reader = threading.get_ident()

    def interleaved_get(kind, key):
        item = original_get(kind, key)
        if (
            threading.get_ident() == reader
            and kind == "model_downloads"
            and key == job_id
            and not read.is_set()
        ):
            read.set()
            assert done.wait(scaled(5)), "finisher did not commit"
        return item

    def finish():
        try:
            assert read.wait(scaled(5)), "reader did not reach its snapshot"
            models._finish(store, job_id, lease, config.name, finished)
        except (AssertionError, KeyError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)
        finally:
            close_thread_connections()
            done.set()

    monkeypatch.setattr(store, "get", interleaved_get)
    worker = threading.Thread(target=finish)
    worker.start()
    try:
        observed = models.download(store, job_id)
    finally:
        worker.join(scaled(6))
    assert not worker.is_alive()
    assert not errors, errors
    assert observed["status"] == "running", observed
    assert "config" not in observed
    assert models.download(store, job_id)["status"] == finished


@pytest.mark.parametrize("lease_state", ["missing", "expired"])
def test_unfinished_download_without_a_live_lease_is_still_interrupted(
    tmp_path, lease_state
):
    store = Store(tmp_path)
    config = ProviderConfig(
        name="ollama",
        kind="ollama",
        base_url="http://127.0.0.1:11434",
        model="fixture",
        allow_localhost=True,
    )
    job_id = "model-orphan"
    store.put(
        "model_downloads",
        job_id,
        {"id": job_id, "status": "running", "config": config.model_dump()},
    )
    if lease_state == "expired":
        with store.connect() as db:
            db.execute(
                "INSERT INTO leases VALUES (?,?,?)",
                (models._lease(config), job_id, time.time() - 1),
            )
    result = models.download(store, job_id)
    assert result["status"] == "interrupted"
    assert "config" not in result
