"""A valid marvel0 site file on temporary paths, and the fake machine trees around
it (an openpi checkout, a checkpoints folder), for the tests of the marvel0 site,
model catalog, command renderer and policy deploy. Nothing here touches the
real machine: every path is below the ``tmp_path`` the caller hands in.

``sample(tmp_path)`` is the site as a dict (one entry per TOML section, the
shape ``levi.marvel0.site.parse`` takes); ``write_site`` writes it as a TOML file
(``override`` replaces whole keys of a section: ``write_site(tmp_path,
safety={"max_xy": 0.5})`` changes one key, ``write_site(tmp_path, gpu=None)``
drops a section); ``make_site`` parses it.
"""

import json
import os
from pathlib import Path

from levi.marvel0 import site as marvel0_site

# What the checkpoints folder of the maintainer's machine holds (read-only
# listing, 2026-10-19). A name maps to the layout ``make_checkpoint`` builds.
REAL_TREE = {
    "pi05_fr3_all_step49999": {},
    "recap_cfg_r1_step10000_jax": {"config": "pi05_fr3_all_state_cfg", "step": 10000},
    "recap_cfg_r2_best_step14300_jax": {
        "config": "pi05_fr3_all_state_cfg",
        "step": 14300,
        "variant": "best",
    },
    "recap_cfg_r2_step15000_jax": {
        "config": "pi05_fr3_all_state_cfg",
        "step": 15000,
    },
    "recap_cfg_r2_best_step_14300": {"kind": "pytorch"},
    "recap_global_step_10000": {"kind": "pytorch"},
    # params/ and assets/norm_stats.json, no top-level norm_stats.json, no
    # config or role: the folder the catalog must leave out.
    "reset_r1_step20000": {"top_norm": False, "assets_norm": True},
}


def sample(tmp_path) -> dict:
    root = Path(tmp_path)
    return {
        "schema": marvel0_site.SCHEMA,
        "paths": {
            "openpi_checkout": str(root / "openpi"),
            "python": str(root / "openpi" / ".venv" / "bin" / "python"),
            "checkpoints_root": str(root / "openpi" / "checkpoints"),
            "rollout_root": str(root / "rollouts"),
            "gpu_lock_file": str(root / "hub" / ".gpu.lock"),
        },
        "live": {"unit": "levi-live.service"},
        "ros": {"path_prefixes": [str(root / "ros"), str(root / "franka_control")]},
        "server": {
            "port": 8000,
            "remote_host": "127.0.0.1",
            "robot_server_url": "http://127.0.0.1:5000/",
        },
        "cameras": {"view": "262622274256", "wrist": "262622275490"},
        "home": {
            "pose": [0.481902, -0.011145, 0.235951, 3.119139, 0.008288, -0.883962],
            "duration_s": 3.0,
        },
        "safety": {
            "min_z_floor": 0.025,
            "min_z_default": 0.025,
            "max_xy": 0.85,
            "max_step_m": 0.10,
            "max_step_rad": 0.35,
            "rx_range": [1.50, 4.03],
            "ry_range": [-0.74, 0.96],
            "rz_range": [-2.44, 0.77],
            "binarize_gripper": True,
            "require_matching_server": True,
            "allow_unknown_prompt": False,
        },
        "caps": {
            "max_steps_default": 500,
            "max_steps_cap": 3000,
            "eval_num_default": 5,
            "eval_num_cap": 50,
            "rtc_horizon_default": 6,
            "rtc_horizon_max": 8,
            "open_loop_default": 6,
            "open_loop_max": 10,
            "prompt_max_chars": 300,
            "note_max_chars": 200,
            "cfg_scale_default": 1.0,
            "cfg_scale_max": 1.5,
        },
        "gpu": {
            "fraction_presets": [0.22, 0.30, 0.45],
            "margin_mib": 1024,
            "lock_hold_max_s": 600,
        },
        "unit": {
            "nice": 10,
            "cpu_affinity": "8-15",
            "cpu_quota_percent": 0,
            "stop_timeout_s": 90,
            "start_timeout_s": 600,
        },
        "catalog": {
            "known_configs": ["pi05_fr3_all_state", "pi05_fr3_all_state_cfg"],
            "cfg_configs": ["pi05_fr3_all_state_cfg"],
            "restricted_prefixes": ["pi05_fr3_sp"],
            "verified": ["pi05_fr3_all_step49999"],
            "excluded": ["reset_r1_step20000"],
        },
        "prompts": {
            "default": "pick the eggplant in the blue plate",
            "known": [
                "pick screws of same size into the empty slot of screw box",
                "stack the plates of same color together",
                "pick the eggplant in the blue plate",
                "stack red plate on white plate then place eggplant on blue plate",
                "place tomato on red plate then eggplant on blue plate",
            ],
        },
        "source": {
            "hash_files": [
                "scripts/serve_policy_rtc.py",
                "scripts/serve_policy.py",
                "src/openpi/policies/rtc_policy.py",
                "src/openpi/policies/policy_config.py",
                "src/openpi/training/config.py",
            ],
            "manifest": "",
        },
    }


def merged(tmp_path, **override) -> dict:
    """``sample`` with ``override`` applied: a dict value updates the section's
    keys, ``None`` drops the section, anything else replaces the top-level key."""
    data = sample(tmp_path)
    for name, value in override.items():
        if value is None:
            data.pop(name, None)
        elif isinstance(value, dict) and isinstance(data.get(name), dict):
            data[name] = {**data[name], **value}
        else:
            data[name] = value
    return data


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    return "[" + ", ".join(_toml_value(v) for v in value) + "]"


def dump_toml(data: dict) -> str:
    """The TOML text of a dict of scalars, lists and one level of tables."""
    lines = [
        f"{k} = {_toml_value(v)}" for k, v in data.items() if not isinstance(v, dict)
    ]
    for name, table in data.items():
        if isinstance(table, dict):
            lines += ["", f"[{name}]"]
            lines += [f"{k} = {_toml_value(v)}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def write_site(tmp_path, name="site.toml", **override) -> Path:
    """The sample site (with ``override``) written to ``<tmp_path>/<name>``,
    mode 0644 like a file a person made."""
    path = Path(tmp_path) / name
    path.write_text(dump_toml(merged(tmp_path, **override)))
    os.chmod(path, 0o644)
    return path


def make_site(tmp_path, **override):
    """The ``Site`` the sample (with ``override``) parses to."""
    return marvel0_site.parse(merged(tmp_path, **override), "env")


def make_openpi_tree(tmp_path) -> Path:
    """A fake openpi checkout: the two serving scripts, the files the site
    lists for hashing, and ``.venv/bin/python`` (an executable file)."""
    base = Path(tmp_path) / "openpi"
    for rel in sample(tmp_path)["source"]["hash_files"]:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# fake {rel}\n")
    python = base / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (base / "checkpoints").mkdir(exist_ok=True)
    return base


def make_checkpoint(
    root,
    name,
    *,
    kind="jax",
    config=None,
    step=None,
    variant=None,
    role=None,
    top_norm=True,
    assets_norm=False,
    assets=True,
):
    """One fake checkpoint folder under ``root``. ``jax`` has ``params/``,
    ``pytorch`` has ``actor/``. VERSION.json is written when it has something
    to say (config, step, variant or role)."""
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    if kind == "jax":
        (folder / "params").mkdir(exist_ok=True)
        (folder / "params" / "weights").write_text("w")
    else:
        (folder / "actor").mkdir(exist_ok=True)
    if assets:
        (folder / "assets").mkdir(exist_ok=True)
    if top_norm:
        (folder / "norm_stats.json").write_text("{}")
    if assets_norm:
        (folder / "assets").mkdir(exist_ok=True)
        (folder / "assets" / "norm_stats.json").write_text("{}")
    version = {
        k: v
        for k, v in (
            ("config", config),
            ("step", step),
            ("role", variant),
            ("policy_role", role),
        )
        if v is not None
    }
    if version:
        (folder / "VERSION.json").write_text(json.dumps(version))
    return folder


def make_checkpoints(tmp_path, names=tuple(REAL_TREE)) -> Path:
    """The fake ``openpi/checkpoints`` folder with the named entries of
    ``REAL_TREE``; returns the folder."""
    root = Path(tmp_path) / "openpi" / "checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        make_checkpoint(root, name, **REAL_TREE[name])
    return root
