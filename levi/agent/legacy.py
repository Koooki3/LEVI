"""Adapters putting existing editors onto the same bundle transaction.

Legacy datasets remain in their original layout until their first Agent commit.
After opt-in, legacy writes require the revision observed by that editor.
"""

import functools
import inspect
import json
import shutil
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

from fastapi import HTTPException

from .store import (
    Conflict,
    Store,
    annotation_digest,
    current_pin,
    dataset_lock,
    digest,
    pin,
)

expected_revision: ContextVar[str | None] = ContextVar(
    "expected_annotation_revision", default=None
)
request_key: ContextVar[str | None] = ContextVar("annotation_request_key", default=None)


@contextmanager
def transaction(
    state, dataset, *, expected=None, key=None, request=None, read_only=False
):
    if current_pin(state, dataset) is not None:
        yield "nested"
        return
    store = Store(state)
    current = store.head(dataset)
    if current == "legacy" or read_only:
        if current != "legacy":
            with pin(state, dataset, store.bundle(dataset, current)):
                yield current
        else:
            yield current
        return
    if expected is None:
        raise HTTPException(428, "Annotation revision required; reload the editor")
    if expected != current:
        raise HTTPException(409, "Annotation revision changed; reload before saving")
    before = annotation_digest(state, dataset)
    base, revision, folder = store.prepare(dataset)
    try:
        with pin(state, dataset, folder):
            yield revision
            unchanged = annotation_digest(state, dataset) == before
        if unchanged:
            shutil.rmtree(folder)
            return
        store.publish(
            dataset,
            base,
            revision,
            key or uuid.uuid4().hex,
            request or digest([dataset, expected]),
            {"revision": revision},
        )
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc


def editor(*, read_only=False, internal=False):
    """Wrap sync FastAPI endpoints while retaining their introspectable signature."""

    def decorate(fn):
        signature = inspect.signature(fn, eval_str=True)

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            from backend import app

            values = signature.bind(*args, **kwargs).arguments
            payload = (
                values.get("payload") or values.get("request") or values.get("req")
            )
            fields = (
                {**values, **payload.model_dump()}
                if hasattr(payload, "model_dump")
                else values
            )
            ref = app.DatasetRef(
                **{k: fields.get(k) for k in ("repo_id", "revision", "local_path")}
            )
            state = app._ensure_state(ref)
            store = Store(app.STATE)
            head = store.head(state.display_slug)
            expected = head if internal else expected_revision.get()
            try:
                lock = (
                    nullcontext()
                    if read_only
                    or current_pin(app.STATE, state.display_slug) is not None
                    else dataset_lock(app.STATE, state.display_slug)
                )
                with lock:
                    with transaction(
                        app.STATE,
                        state.display_slug,
                        expected=expected,
                        read_only=read_only,
                    ) as revision:
                        response = fn(*args, **kwargs)
                    observed = revision if read_only else store.head(state.display_slug)
                response.headers["X-LEVI-Annotation-Revision"] = observed
                return response
            except Conflict as exc:
                raise HTTPException(409, str(exc)) from exc

        wrapped.__signature__ = signature
        return wrapped

    return decorate


def response_json(response):
    return json.loads(response.body)
