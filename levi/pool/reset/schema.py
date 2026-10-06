"""Options and records of a reset export."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DIRECTIONS = ("forward_only", "forward_and_reset", "reset_only")
RELEASE_CLASSES = ("in_place", "in_reach", "escaped", "unknown")
GENERATION = ("reversed_source", "reversed_source_seam", "recorded_bridge", "partial")
SCHEMA = "levi.pool.reset.v1"
DEFAULT_TEMPLATE = "Reset: {task}"


class BridgeLink(BaseModel):
    """A recorded episode that carries the part of a reset the forward
    episode cannot (a real approach and grasp of the object where it came to
    rest). Both are pool episodes, named by their pool keys."""

    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1)  # the forward episode (pool key)
    record: str = Field(min_length=1)  # the recorded reset stretch (pool key)


class ResetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    # forward_only: no reset episodes (the default, the export as it always was).
    direction: Literal["forward_only", "forward_and_reset", "reset_only"] = (
        "forward_only"
    )
    # The instruction of a reset episode; {task} is the forward task text the
    # export would write (after task_text and approved corrections).
    task_template: str = Field(DEFAULT_TEMPLATE, min_length=1, max_length=400)
    # The action contract (levi.counterfactual.schema) the sources follow.
    action_contract: str = "fr3-robotiq@1"
    # The worst release a reversed episode may contain: "in_place" (the object
    # stayed put: reversal is physically valid as it is), or "in_reach" (it
    # settled between the open fingers: the frames of its fall are cut out).
    max_release: Literal["in_place", "in_reach"] = "in_reach"
    # What happens to an episode that cannot be reversed whole: leave it out
    # ("exclude"), or keep the part from its last safe hold on ("partial": the
    # reset then starts with the object in the gripper; flagged in the record).
    on_ineligible: Literal["exclude", "partial"] = "exclude"
    # Rest frames (the arm still, the fingers open) that must agree before an
    # object counts as at rest. One frame proves nothing: at 10 Hz a falling
    # object is often not blurred. 1 trusts a single frame (more episodes,
    # some of them wrong); the default is the safe one.
    min_settled_rows: int = Field(2, ge=1, le=6)
    # An episode with no grasp (pushing, pouring, wiping) is not reversed: its
    # reversal is physically meaningless and the signals cannot tell.
    allow_no_grasp: bool = False
    # Only demonstrations that finished the task are reversed.
    require_forward_success: bool = True
    # The camera the object is looked for in after a release.
    release_camera: str = "observation.images.hand"
    # Rows by which the reversed gripper command leads the visible finger
    # motion when the capture has no measured width.
    gripper_lead_rows: int = Field(3, ge=0, le=15)
    # A local vision model connection (name) that may veto an accepted release.
    review_model: str | None = Field(None, min_length=1, max_length=100)
    # Recorded stretches for episodes that cannot be reversed.
    bridges: list[BridgeLink] = Field(default_factory=list)

    @field_validator("task_template")
    @classmethod
    def _template(cls, value):
        if value.count("{task}") != 1 or "{" in value.replace("{task}", ""):
            raise ValueError("task_template must contain {task} exactly once")
        return value

    @property
    def enabled(self) -> bool:
        return self.direction != "forward_only"

    @property
    def writes_forward(self) -> bool:
        return self.direction != "reset_only"

    def looks_reset(self, text: str) -> bool:
        """Whether ``text`` already has this template's shape (``Reset: …``):
        the forward text of an episode that is itself a reset."""
        before, _, after = self.task_template.partition("{task}")
        if not (before or after):
            return False
        return (
            len(text) > len(before) + len(after)
            and text.startswith(before)
            and text.endswith(after)
        )

    def reset_text(self, forward_text: str) -> str:
        return self.task_template.replace("{task}", forward_text)
