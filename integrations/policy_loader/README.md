# policy_loader: serve a policy behind a loader module

`serve_loader.py` serves any policy on the FR3 client's websocket contract with the identity LEVI's policy switch checks
(`--config`, `--checkpoint-dir` in argv and in the metadata). `reset_r1_loader.py` is the loader of the reset_r1
checkpoint. `install.py` copies both into an openpi checkout's git-excluded `lab/policy_loader/` (dry run by default;
this repository never runs it by itself). Design, argv for a binding, install steps and open items:
[docs/POLICY_LOADER.md](../../docs/POLICY_LOADER.md) ([中文](../../docs/POLICY_LOADER.zh-CN.md)).

Tests (no GPU, no checkpoint, no socket): `tests/test_policy_loader.py`.
