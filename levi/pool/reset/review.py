"""A second look by a local vision model, which can only say no.

The image measures in ``vision`` accept a release when the object is found
again near the middle of the wrist camera. They know nothing about objects, so
a plain background can fool them. A local vision-language model is asked, with
the hold frame and the rest frame, whether the object is still within the open
fingers' reach and at rest. Its answer is used one way only: a clear "no"
turns an accepted release into ``escaped`` (``vlm_veto``). A "yes" changes
nothing and a release the measures could not place is not rescued by it:
published benchmarks of vision-language models judging whether a manipulation
worked put them near 0.6-0.8 balanced accuracy, biased towards "it worked", so
they are good for catching a mistake and not good enough to make a data
decision alone.

The call goes to the local model connection named in the options and is
refused while another process computes on the GPU (``levi.inference.gpu``).
"""

import base64
import json

import cv2

from ...conversion import media

QUESTION = (
    "Two photos from the camera on a robot hand. Photo 1: the gripper is closed "
    "on an object. Photo 2: the same hand pose after the gripper opened and the "
    "object was let go. Is the object still between or right under the open "
    "fingers, at rest, so that closing the gripper again would pick it up? "
    "Answer object_in_reach true if yes, false if it fell or rolled out of the "
    "fingers' reach or is gone from view, null if you cannot tell."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "object_in_reach": {"type": ["boolean", "null"]},
        "note": {"type": "string", "maxLength": 200},
    },
    "required": ["object_in_reach", "note"],
    "additionalProperties": False,
}


def _png(frame) -> str:
    ok, data = cv2.imencode(".png", frame)
    if not ok:
        raise ValueError("cannot encode a frame")
    return base64.b64encode(data.tobytes()).decode("ascii")


class VlmReviewer:
    """``reviewer(evidence, hold_row, rest_row, record) -> dict``; the
    ``analysis`` module calls it for every accepted release."""

    def __init__(self, config, client=None):
        self.config = config
        self.client = client
        self.calls = 0

    def _client(self):
        if self.client is None:
            from ...inference.provider import client_for

            self.client = client_for(self.config, timeout=120)
        return self.client

    def __call__(self, ev, hold_row: int, rest_row: int, record: dict) -> dict | None:
        if ev.release_video is None:
            return None
        frames = media.frames_at(ev.release_video, [hold_row, rest_row])
        message = {
            "role": "user",
            "content": QUESTION,
            "images": [_png(frames[hold_row]), _png(frames[rest_row])],
        }
        if self.client is None:
            from ...inference.gpu import require_free

            require_free(self.config)
        reply = self._client().chat(
            self.config.model,
            self.config.model_digest,
            [message],
            output_schema=SCHEMA,
            max_output_tokens=200,
            context_tokens=self.config.context_tokens,
            think=False,
        )
        self.calls += 1
        try:
            answer = json.loads(reply["content"])
        except (TypeError, ValueError):
            return None
        if not isinstance(answer, dict) or "object_in_reach" not in answer:
            return None
        return {
            "object_in_reach": answer["object_in_reach"],
            "note": str(answer.get("note", ""))[:200],
            "model": self.config.model,
            "model_digest": self.config.model_digest,
        }


def open_reviewer(name: str) -> VlmReviewer:
    """The reviewer for a local model connection of this workspace."""
    from ...agent.store import Store
    from ...inference import models
    from .. import settings

    config = models.configuration(Store(settings.workspace()), name)
    if not (config.vision and config.structured_output and config.model_digest):
        raise ValueError(
            f"Model connection {name!r} needs a bound model digest with vision "
            "and structured output (see docs/VLLM.md)"
        )
    return VlmReviewer(config)
