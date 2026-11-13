"""Synthetic scenarios for the offline reset harness; no hardware inventory."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    first_grasp_empty: bool = False
    holding_unknown: bool = False
    stale_observation: bool = False
    handoff_mismatch: bool = False
    reachable: bool = True
    stop_before_action: bool = False


SCENARIOS = {
    item.id: item
    for item in (
        Scenario("normal_random"),
        Scenario("empty_grasp_retry", first_grasp_empty=True),
        Scenario("held_unknown", holding_unknown=True),
        Scenario("stale_obs", stale_observation=True),
        Scenario("handoff_mismatch", handoff_mismatch=True),
        Scenario("unreachable", reachable=False),
        Scenario("stop", stop_before_action=True),
    )
}


def scenario_catalog() -> list[dict]:
    return [
        {"id": key, "label_key": f"resetHarness.scenarios.{key}"} for key in SCENARIOS
    ]
