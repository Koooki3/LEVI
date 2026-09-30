"""An unattended evaluation leaves the verdict to LEVI: the client's
placeholder success_flag_final of 0 must never read as a recorded failure."""

import json

import pytest

from levi.conversion import raw


def demo(tmp_path, meta):
    folder = tmp_path / "demo_0000"
    folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps(meta))
    return folder


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"data_source": "policy_rollout", "eval": {"outcome": "success"}}, "success"),
        ({"data_source": "policy_rollout", "eval": {"outcome": "failure"}}, "failure"),
        # Unattended: no verdict yet, whatever the placeholder flag says.
        (
            {
                "data_source": "policy_rollout",
                "success_flag_final": 0,
                "eval": {"outcome": "unlabeled"},
            },
            None,
        ),
        (
            {
                "data_source": "policy_rollout",
                "success_flag_final": 0,
                "eval": {"outcome": "aborted"},
            },
            None,
        ),
        # Rollouts from before the eval block keep their old reading.
        ({"data_source": "policy_rollout", "success_flag_final": 1}, "success"),
        ({"data_source": "policy_rollout", "success_flag_final": 0}, "failure"),
        ({"data_source": "teleop", "success_flag_final": 1}, None),
    ],
)
def test_demo_outcome(tmp_path, meta, expected):
    assert raw.demo_outcome(demo(tmp_path, meta)) == expected
