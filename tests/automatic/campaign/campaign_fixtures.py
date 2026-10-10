"""Campaign job files for the tests (written into a test's folder)."""

from pathlib import Path

CONTRACT = """initial_state:
  id: eggplant-initial
  version: "1"
  predicates:
    required: [object_at_source, gripper_open]
  observations:
    require_visible_evidence: true
    min_evidence_refs: 1
"""

PAIRING = """  pairing:
    recap_cfg_*: pi05_fr3_all_state_cfg
    pi05_fr3_all_step*: pi05_fr3_all_state
"""

ARM_A = """    A:
      role: reference
      policy_forward:
        checkpoint_dir: /ckpt/pi05_fr3_all_step49999
        config: pi05_fr3_all_state
        port: 8000
      checkpoint:
        sha256_status: recorded
        manifest_sha256: {sha}
      versions: SFT step49999
"""

ARM_B = """    B:
      policy_forward:
        checkpoint_dir: /ckpt/recap_cfg_r2_best_step14300_jax
        config: pi05_fr3_all_state_cfg
        cfg_scale: 1.0
        port: 8000
"""

ARM_C = """    C:
      policy_forward:
        checkpoint_dir: /ckpt/recap_cfg_r1_step10000_jax
        config: pi05_fr3_all_state_cfg
        port: 8000
"""

ARM_D = """    D:
      policy_forward:
        checkpoint_dir: /ckpt/recap_cfg_r2_step15000_jax
        config: pi05_fr3_all_state_cfg
        port: 8000
"""


def layouts_text(n: int) -> str:
    lines = ["schema_version: levi.aeri.layouts.v1", "cards:"]
    for i in range(1, n + 1):
        lines += [
            f"  c{i:02d}:",
            f"    description: layout {i}",
            "    predicates:",
            "      object_at_source: true",
            "    params:",
            f"      x_cm: {i * 2}",
        ]
    return "\n".join(lines) + "\n"


def job_text(
    *,
    campaign_id="c1",
    strategy="human_assisted",
    kind="counterbalanced_segments",
    segment_trials=2,
    trials=4,
    seed=7,
    arms=(ARM_A, ARM_B),
    layouts="  layouts:\n    source: card_set\n    file: layouts.yaml\n",
    pairing=PAIRING,
    extra_campaign="",
    termination="  min_steps: 5\n",
    extra_top="",
    robot="fr3",
) -> str:
    reset = f"reset:\n  strategy: {strategy}\n"
    policies = "policies:\n  forward:\n    max_steps: 120\n"
    if strategy != "human_assisted":
        policies += "  reset:\n    max_steps: 60\n"
        reset += "  max_attempts: 1\n"
    segment = (
        "" if segment_trials is None else f"    segment_trials: {segment_trials}\n"
    )
    return (
        "schema_version: levi.aeri.job.v1\n"
        "experiment:\n  name: base\n  random_seed: 3\n"
        f"{policies}"
        "task:\n  instruction: put the eggplant in the bowl\n"
        "  initial_state_spec: initial-state.yaml\n"
        f"termination:\n{termination}"
        f"{reset}"
        "recording:\n  rollout_root: rollouts\n"
        f"{extra_top}"
        "campaign:\n"
        f"  id: {campaign_id}\n"
        f"  robot: {robot}\n"
        f"  trials_per_arm: {trials}\n"
        "  schedule:\n"
        f"    kind: {kind}\n{segment}"
        f"    seed: {seed}\n"
        f"{layouts}{pairing}{extra_campaign}"
        "  arms:\n" + "".join(arm.format(sha="ab" * 32) for arm in arms)
    )


def write_job(folder: Path, cards: int = 6, name="campaign.yaml", **over) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rollouts").mkdir(exist_ok=True)
    (folder / "initial-state.yaml").write_text(CONTRACT)
    (folder / "layouts.yaml").write_text(layouts_text(cards))
    path = folder / name
    path.write_text(job_text(**over))
    return path


class FakePlanner:
    """Plans a child as the sha256 of its bytes (no real job loader)."""

    def __init__(self):
        self.calls = []

    def plan(self, job_path):
        import hashlib

        data = Path(job_path).read_bytes()
        self.calls.append(Path(job_path).name)
        return hashlib.sha256(data).hexdigest(), {"file": Path(job_path).name}
