# Policy loader service (`integrations/policy_loader`)

A generic policy server entry for services that LEVI's FR3 policy switch (`levi/automatic/fr3_policy_swap.py`) starts from a frozen argv, with the first loader: the reset_r1 checkpoint. Design: the step-5 reset design (R1), `lab/levi-hub/programs/levi2/STEP5-AERI-RESET-DESIGN-20261114.md`. This page covers the service; the job and run side (`subtask.reverse_policy_role`) is in [AUTOMATIC_PIPELINE.md](AUTOMATIC_PIPELINE.md).

## Why

The switch checks a service twice: passively, that its argv names the frozen `config` and the absolute `checkpoint_dir` as separate arguments without whitespace (`fr3_policy_swap._service`, `campaign.switch.runs_arm`), and at the handshake, that the metadata's `config` equals the deployment's and its `checkpoint_dir` has the same real path (`fr3_bridge.check_identity`). The 2026-11-14 prototype server (`openpi/lab/reset_r1_serve/serve_reset.py`) reported `"reset_r1_step20000 (lab/reset_r1_serve)"` and no `checkpoint_dir`, took only `--port` and `--host`, and so passed neither. `serve_loader.py` takes both identity values as arguments and reports them.

## What it does

```
python lab/policy_loader/serve_loader.py --config reset_r1_step20000 \
    --checkpoint-dir <absolute checkpoint folder> --loader reset_r1_loader:load --port <N> [--check]
```

- `--config`: a code-style name (letters, digits, `_ . -`; no space, no parenthesis). `--checkpoint-dir`: absolute, no `..`. `--port` has no default: the caller owns it. 5000, 5001, 5100 and 7470 are refused; the host must be loopback.
- The loader module (`module:function`, imported from the folder of `serve_loader.py`) defines `CONTRACT` and a function `load(checkpoint_dir) -> policy`. `CONTRACT` must hold `rebase_euler_dim` 3, `rebase_euler_offset` +-pi, `action_dim` 7, `action_horizon` (1 to 64), `ee_frame` `franka_hand_tcp` and `discrete_state_input` (true or false), and may hold `gripper_convention` and `supports_rtc`; anything else is refused (fail closed). These are the checks of `run_robotiq_client._check_server`, made before any model loads; `ee_frame` is required here, not only "not different". `CONTRACT` must be importable without a GPU: the heavy imports belong inside `load`.
- Metadata served to every client: the contract plus `config` and `checkpoint_dir` (real path) from the arguments. A loader cannot set those two.
- Each request is checked: the four observation keys, `observation/state` 7 finite numbers, a non-empty prompt; `rtc_prev_actions` is refused unless the contract says `supports_rtc`. Each answer must be a finite `(action_horizon, >=7)` chunk, cut to 7 columns.
- `--check` runs the argument, loader-import and contract checks and prints the metadata; it loads no model, imports no openpi and opens no socket, so it needs no GPU.

`reset_r1_loader.py` renames the wire keys to the training names and leaves the Euler rebase and the delta conversion to the checkpoint's own transforms (its README: "do not add an rx +-pi or a delta inverse"). It needs the unpacked OpenPI source of the checkpoint (`runtime/openpi_reset_source.tar.gz`) in `$RESET_R1_OPENPI_ROOT` (default `openpi_reset` next to the installed file); caches go to `$RESET_R1_RUN_DIR` (default `run` next to it). The reset model understands only its training sentences ("Reset: ..."); LEVI freezes them in the job (`subtask.reset_prompts`).

## Binding argv

The service block of a binding's `policy_swap.services.reset` (all values in separate arguments, no whitespace):

```
[<openpi .venv python>, lab/policy_loader/serve_loader.py,
 --config, reset_r1_step20000, --checkpoint-dir, <absolute reset_r1 folder>,
 --loader, reset_r1_loader:load, --port, <the binding's reset port>]
```

with the same `config` and `checkpoint_dir` in `deployments.reset` of the job. `tests/test_policy_loader.py` runs such an argv through the real `fr3_policy_swap._service` and `runs_arm`, and the served metadata through the real `check_identity`.

## Installing into openpi (not done by this repository)

```
python3 integrations/policy_loader/install.py --openpi-root <openpi checkout>           # dry run
python3 integrations/policy_loader/install.py --openpi-root <openpi checkout> --apply
```

It writes only `lab/policy_loader/{serve_loader,reset_r1_loader}.py` and `installation-receipt.json` (`lab/` of an openpi checkout is git-excluded; no existing openpi file is edited), refuses symlinks and different existing files, and needs `src/openpi/serving/websocket_policy_server.py` and `scripts/serve_policy.py` to exist. Run it with the openpi checkout's owner's agreement; then unpack the checkout's source tarball as above.

## Open

- GPU smoke test (not done): start `serve_loader.py` with the real checkpoint on a loopback port the user names, under the GPU lock at `XLA_PYTHON_CLIENT_MEM_FRACTION=.22`, send one recorded observation, compare the first chunk with the prototype's, stop it. No robot is involved.
- The client mirror `serve_loader.client_problems` copies `_check_server` by hand; if the client changes, the test cannot see it.
- Real-robot validation V1 to V5 of the design is not done.
