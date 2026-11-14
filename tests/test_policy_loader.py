"""integrations/policy_loader: the generic policy server entry and the reset_r1 loader (design STEP5-AERI-RESET-DESIGN,
R1 / P1a). Pure CPU: no GPU, no checkpoint, no socket, no openpi. The identity and argv tests run the real checks of
``levi.automatic`` (``fr3_bridge.check_identity``, ``fr3_policy_swap._service``) against what the server reports."""

import importlib
import math
import sys
from pathlib import Path

import numpy as np
import pytest

LOADER_DIR = Path(__file__).resolve().parents[1] / "integrations" / "policy_loader"
sys.path.insert(0, str(LOADER_DIR))
serve_loader = importlib.import_module("serve_loader")
reset_r1_loader = importlib.import_module("reset_r1_loader")

from levi.automatic import fr3_bridge as bridge
from levi.automatic import fr3_policy_swap as swap
from levi.automatic.campaign.switch import runs_arm

CONFIG = "reset_r1_step20000"


def contract(**over):
    base = dict(reset_r1_loader.CONTRACT)
    base.update(over)
    return base


@pytest.fixture
def ckpt(tmp_path):
    folder = tmp_path / "checkpoints" / CONFIG
    folder.mkdir(parents=True)
    return folder


def write_loader(tmp_path, monkeypatch, name, body):
    folder = tmp_path / "loaders"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.delitem(sys.modules, name, raising=False)
    return folder


GOOD_LOADER = """
import numpy as np
CONTRACT = {contract!r}
def load(checkpoint_dir):
    class P:
        def infer(self, obs):
            return {{"actions": np.zeros((10, 8), dtype=np.float32)}}
    return P()
"""


# --- the identity the swap and the handshake check -----------------------------------------------


def test_metadata_carries_the_identity_the_handshake_compares(ckpt):
    meta = serve_loader.metadata_for(CONFIG, str(ckpt), contract())
    assert meta["config"] == CONFIG
    assert meta["checkpoint_dir"] == str(ckpt.resolve())
    # fr3_bridge.check_identity is the real handshake check (fr3_bridge.py: policy deployment identity).
    bridge.check_identity({"config": CONFIG, "checkpoint_dir": str(ckpt)}, meta)


def test_a_different_checkpoint_or_config_fails_the_handshake(ckpt, tmp_path):
    meta = serve_loader.metadata_for(CONFIG, str(ckpt), contract())
    with pytest.raises(ValueError, match="identity mismatch"):
        bridge.check_identity(
            {"config": CONFIG, "checkpoint_dir": str(tmp_path / "other")}, meta
        )
    with pytest.raises(ValueError, match="identity mismatch"):
        bridge.check_identity({"config": "other", "checkpoint_dir": str(ckpt)}, meta)


def test_a_symlinked_checkpoint_is_reported_by_its_real_path(ckpt, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(ckpt)
    meta = serve_loader.metadata_for(CONFIG, str(link), contract())
    bridge.check_identity({"config": CONFIG, "checkpoint_dir": str(ckpt)}, meta)


def service_raw(tmp_path, ckpt, *, port=8001, config=CONFIG):
    return {
        "unit": "levi-aeri-policy-reset",
        "argv": [
            sys.executable,
            str(LOADER_DIR / "serve_loader.py"),
            "--config",
            config,
            "--checkpoint-dir",
            str(ckpt),
            "--loader",
            "reset_r1_loader:load",
            "--port",
            str(port),
        ],
        "working_directory": str(tmp_path),
        "mem_fraction": 0.22,
    }


def test_the_argv_passes_the_swap_service_and_passive_identity_checks(tmp_path, ckpt):
    deployment = {"config": CONFIG, "checkpoint_dir": str(ckpt)}
    service = swap._service(
        "reset",
        service_raw(tmp_path, ckpt),
        endpoint={"port": 8001},
        deployment=deployment,
    )
    assert service.config == CONFIG and service.checkpoint_dir == str(ckpt)
    assert runs_arm(list(service.argv), service.arm())


def test_a_config_with_a_space_cannot_be_served_or_swapped(tmp_path, ckpt):
    # The prototype's "reset_r1_step20000 (lab/reset_r1_serve)" was neither: a space splits ExecStart.
    spaced = "reset_r1_step20000 (lab/reset_r1_serve)"
    with pytest.raises(SystemExit):
        serve_loader.parse_args(
            [
                "--config",
                spaced,
                "--checkpoint-dir",
                str(ckpt),
                "--loader",
                "reset_r1_loader:load",
                "--port",
                "8001",
            ]
        )
    with pytest.raises(ValueError, match="whitespace"):
        swap._service(
            "reset",
            service_raw(tmp_path, ckpt, config=spaced),
            endpoint={"port": 8001},
            deployment={"config": spaced, "checkpoint_dir": str(ckpt)},
        )
    with pytest.raises(serve_loader.LoaderError, match="code-style"):
        serve_loader.metadata_for(spaced, str(ckpt), contract())


# --- the client contract ------------------------------------------------------------------------


def test_the_reset_r1_contract_is_what_the_prototype_and_the_client_need():
    c = serve_loader.check_contract(reset_r1_loader.CONTRACT)
    assert c["rebase_euler_dim"] == 3
    assert math.isclose(abs(c["rebase_euler_offset"]), math.pi, abs_tol=1e-9)
    assert (c["action_dim"], c["action_horizon"]) == (7, 10)
    assert c["ee_frame"] == "franka_hand_tcp" and c["discrete_state_input"] is True
    assert serve_loader.client_problems(c) == []


@pytest.mark.parametrize(
    "key,value,fragment",
    [
        ("rebase_euler_dim", 6, "rebase_euler_dim"),
        ("rebase_euler_offset", 0.0, "rebase_euler_offset"),
        ("rebase_euler_offset", float("nan"), "rebase_euler_offset"),
        ("rebase_euler_offset", True, "rebase_euler_offset"),
        ("action_dim", 8, "action_dim"),
        ("action_dim", True, "action_dim"),
        ("action_horizon", 0, "action_horizon"),
        ("action_horizon", 10.0, "action_horizon"),
        ("ee_frame", "flange", "ee_frame"),
        ("ee_frame", None, "ee_frame"),
        ("discrete_state_input", 1, "discrete_state_input"),
    ],
)
def test_a_contract_the_client_would_refuse_is_refused_at_start(key, value, fragment):
    with pytest.raises(serve_loader.LoaderError, match=fragment):
        serve_loader.check_contract(contract(**{key: value}))


def test_the_negative_pi_offset_is_accepted_like_the_client_does():
    serve_loader.check_contract(contract(rebase_euler_offset=-math.pi))


def test_missing_unknown_and_identity_keys_are_refused():
    base = contract()
    del base["ee_frame"]
    with pytest.raises(serve_loader.LoaderError, match="lacks ee_frame"):
        serve_loader.check_contract(base)
    with pytest.raises(serve_loader.LoaderError, match="does not know: surprise"):
        serve_loader.check_contract(contract(surprise=1))
    with pytest.raises(serve_loader.LoaderError, match="come from the arguments"):
        serve_loader.check_contract(contract(checkpoint_dir="/x"))
    with pytest.raises(serve_loader.LoaderError, match="dict"):
        serve_loader.check_contract(None)
    with pytest.raises(serve_loader.LoaderError, match="supports_rtc"):
        serve_loader.check_contract(contract(supports_rtc="yes"))


def test_supports_rtc_is_not_advertised_in_the_metadata(ckpt):
    meta = serve_loader.metadata_for(CONFIG, str(ckpt), contract(supports_rtc=True))
    assert "supports_rtc" not in meta
    assert serve_loader.client_problems(meta) == []


# --- the arguments -------------------------------------------------------------------------------


def args_for(ckpt, **over):
    base = {
        "--config": CONFIG,
        "--checkpoint-dir": str(ckpt),
        "--loader": "reset_r1_loader:load",
        "--port": "8001",
    }
    base.update({k: str(v) for k, v in over.items()})
    return [x for pair in base.items() for x in pair]


def test_valid_arguments_parse_with_a_loopback_default(ckpt):
    args = serve_loader.parse_args(args_for(ckpt))
    assert (args.host, args.port, args.check) == ("127.0.0.1", 8001, False)


@pytest.mark.parametrize(
    "over",
    [
        {"--port": 5000},
        {"--port": 5001},
        {"--port": 5100},
        {"--port": 7470},
        {"--port": 80},
        {"--port": 70000},
        {"--host": "0.0.0.0"},
        {"--host": "192.168.1.5"},
        {"--checkpoint-dir": "relative/dir"},
        {"--checkpoint-dir": "/models/../etc"},
        {"--loader": "no_colon"},
        {"--loader": "a b:load"},
        {"--config": "-x"},
    ],
)
def test_arguments_that_would_reach_a_robot_port_or_leave_loopback_are_refused(
    ckpt, over
):
    with pytest.raises(SystemExit):
        serve_loader.parse_args(args_for(ckpt, **over))


def test_the_port_has_no_default(ckpt):
    argv = args_for(ckpt)
    del argv[argv.index("--port") : argv.index("--port") + 2]
    with pytest.raises(SystemExit):
        serve_loader.parse_args(argv)


# --- the loader ----------------------------------------------------------------------------------


def test_check_prints_the_metadata_without_loading_a_model_or_a_server(
    tmp_path, ckpt, monkeypatch, capsys
):
    write_loader(
        tmp_path,
        monkeypatch,
        "good_loader",
        GOOD_LOADER.format(contract=contract()).replace(
            "def load(checkpoint_dir):",
            "def load(checkpoint_dir):\n    raise AssertionError('loaded')",
        ),
    )
    monkeypatch.setitem(
        sys.modules, "openpi", None
    )  # an import of openpi would fail the test
    code = serve_loader.main(
        args_for(ckpt, **{"--loader": "good_loader:load"}) + ["--check"]
    )
    assert code == 0
    import json

    meta = json.loads(capsys.readouterr().out)
    assert meta["config"] == CONFIG and meta["checkpoint_dir"] == str(ckpt.resolve())
    assert serve_loader.client_problems(meta) == []


def test_a_loader_without_contract_or_function_or_module_is_refused(
    tmp_path, monkeypatch
):
    write_loader(
        tmp_path, monkeypatch, "no_contract", "def load(d):\n    return None\n"
    )
    with pytest.raises(serve_loader.LoaderError, match="CONTRACT"):
        serve_loader.resolve_loader("no_contract:load", tmp_path / "loaders")
    write_loader(tmp_path, monkeypatch, "no_function", f"CONTRACT = {contract()!r}\n")
    with pytest.raises(serve_loader.LoaderError, match="no callable 'load'"):
        serve_loader.resolve_loader("no_function:load", tmp_path / "loaders")
    with pytest.raises(serve_loader.LoaderError, match="cannot import"):
        serve_loader.resolve_loader("not_there_at_all:load", tmp_path / "loaders")


def test_main_refuses_a_missing_checkpoint_folder(tmp_path, capsys):
    code = serve_loader.main(
        args_for(tmp_path / "missing", **{"--loader": "reset_r1_loader:load"})
        + ["--check"]
    )
    assert code == 2
    assert "not a directory" in capsys.readouterr().err


# --- the served policy ---------------------------------------------------------------------------


class Echo:
    def __init__(self, actions):
        self.actions = actions
        self.seen = []

    def infer(self, obs):
        self.seen.append(obs)
        return {"actions": self.actions}


def obs(**over):
    base = {
        "observation/image": np.zeros((4, 4, 3), np.uint8),
        "observation/wrist_image": np.zeros((4, 4, 3), np.uint8),
        "observation/state": np.zeros(7, np.float32),
        "prompt": "Reset: pick the eggplant into the blue plate",
    }
    base.update(over)
    return base


def served(actions=None, **contract_over):
    c = contract(**contract_over)
    actions = np.zeros((10, 8), np.float32) if actions is None else actions
    return serve_loader.ServedPolicy(Echo(actions), c, {"config": "x"})


def test_a_request_gets_a_ten_by_seven_chunk():
    out = served().infer(obs())
    assert out["actions"].shape == (10, 7) and out["actions"].dtype == np.float32
    assert out["policy_timing"]["infer_ms"] >= 0


def test_rtc_requests_are_refused_unless_the_contract_allows_them():
    with pytest.raises(ValueError, match="plain chunking only"):
        served().infer(obs(rtc_prev_actions=np.zeros((3, 7))))
    served(supports_rtc=True).infer(obs(rtc_prev_actions=np.zeros((3, 7))))


@pytest.mark.parametrize(
    "change",
    [
        {"observation/state": np.zeros(6, np.float32)},
        {"observation/state": np.full(7, np.nan, np.float32)},
        {"prompt": ""},
        {"prompt": None},
    ],
)
def test_a_malformed_observation_never_reaches_the_model(change):
    policy = served()
    with pytest.raises(ValueError):
        policy.infer(obs(**change))
    assert policy._policy.seen == []


def test_a_missing_observation_key_is_named():
    bad = obs()
    del bad["observation/wrist_image"]
    with pytest.raises(ValueError, match="observation/wrist_image"):
        served().infer(bad)


@pytest.mark.parametrize(
    "actions",
    [
        np.zeros((9, 8), np.float32),
        np.zeros((10, 6), np.float32),
        np.zeros(10, np.float32),
        np.full((10, 7), np.inf),
    ],
)
def test_a_chunk_the_client_could_not_use_is_refused(actions):
    with pytest.raises(ValueError):
        served(actions).infer(obs())


# --- the reset_r1 loader module -------------------------------------------------------------------


def test_the_wire_observation_is_only_renamed_for_the_reset_model():
    o = obs()
    t = reset_r1_loader.to_training_observation(o)
    assert set(t) == {
        "observation.images.view1",
        "observation.images.hand",
        "observation.state",
        "prompt",
    }
    assert t["observation.images.view1"] is o["observation/image"]
    assert t["observation.images.hand"] is o["observation/wrist_image"]
    assert t["observation.state"] is o["observation/state"]
    assert t["prompt"] == o["prompt"]


def test_load_checks_the_checkpoint_and_the_unpacked_source_before_importing_anything(
    tmp_path,
):
    with pytest.raises(FileNotFoundError, match="not a reset_r1 checkpoint"):
        reset_r1_loader.load(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "train_reset_r1.py").write_text("")
    with pytest.raises(FileNotFoundError, match="openpi_reset"):
        reset_r1_loader.load(tmp_path)
