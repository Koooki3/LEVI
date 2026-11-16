# 策略加载器服务（`integrations/policy_loader`）

给 LEVI 的 FR3 策略切换（`levi/automatic/fr3_policy_swap.py`）用的通用策略服务入口：切换从冻结的 argv 启动服务；第一个加载器是 reset_r1 检查点。设计：第 5 步复位设计 R1（`lab/levi-hub/programs/levi2/STEP5-AERI-RESET-DESIGN-20261114.md`）。本页只讲服务；作业和运行一侧（`subtask.reverse_policy_role`）见 [AUTOMATIC_PIPELINE.zh-CN.md](AUTOMATIC_PIPELINE.zh-CN.md)。

## 为什么要有它

切换对服务查两次：被动检查，argv 里要把冻结的 `config` 和绝对的 `checkpoint_dir` 写成独立参数且不含空白（`fr3_policy_swap._service`、`campaign.switch.runs_arm`）；握手时，metadata 的 `config` 要等于部署的 `config`，`checkpoint_dir` 的真实路径要相同（`fr3_bridge.check_identity`）。2026-11-14 的原型服务（`openpi/lab/reset_r1_serve/serve_reset.py`）报的是 `"reset_r1_step20000 (lab/reset_r1_serve)"`，没有 `checkpoint_dir`，只收 `--port` 和 `--host`，两项都过不了。`serve_loader.py` 把两个身份值作为参数收进来并如实上报。

## 它做什么

```
python lab/policy_loader/serve_loader.py --config reset_r1_step20000 \
    --checkpoint-dir <检查点文件夹绝对路径> --loader reset_r1_loader:load --port <N> [--check]
```

- `--config`：代码式名字（字母、数字、`_ . -`，不能有空格和括号）。`--checkpoint-dir`：绝对路径，不含 `..`。`--port` 没有默认值，由调用方决定；5000、5001、5100、7470 会被拒绝，host 必须是回环地址。
- 加载器模块（`module:function`，从 `serve_loader.py` 所在文件夹导入）要定义 `CONTRACT` 和函数 `load(checkpoint_dir) -> policy`。`CONTRACT` 必须有 `rebase_euler_dim` 3、`rebase_euler_offset` ±π、`action_dim` 7、`action_horizon`（1 到 64）、`ee_frame` 为 `franka_hand_tcp`、`discrete_state_input`（真或假），可以有 `gripper_convention` 和 `supports_rtc`；其他键一律拒绝（失败即关闭）。这些就是 `run_robotiq_client._check_server` 的检查，在任何模型加载之前做；这里 `ee_frame` 必须写出，不只是“不等于别的”。`CONTRACT` 必须不依赖 GPU 就能导入：重的导入放进 `load`。
- 对每个客户端报的 metadata：契约加上来自参数的 `config` 和 `checkpoint_dir`（真实路径），加载器不能改这两个值。
- 每次请求都检查：四个观测键齐全、`observation/state` 是 7 个有限数、提示词非空；契约没有 `supports_rtc` 时拒收 `rtc_prev_actions`。每次回答必须是有限的 `(action_horizon, >=7)` 块，截成 7 列。
- `--check` 只做参数、加载器导入和契约检查并打印 metadata：不加载模型、不导入 openpi、不开 socket，所以不需要 GPU。

`reset_r1_loader.py` 只把线上的键名改成训练用的名字，欧拉角 rebase 和 delta 转换留给检查点自带的变换（它的 README：“不要另做 rx 加减 π 或 delta 反变换”）。它需要检查点的 OpenPI 源码解包（`runtime/openpi_reset_source.tar.gz`）放在 `$RESET_R1_OPENPI_ROOT`（默认是安装文件旁的 `openpi_reset`）；缓存放 `$RESET_R1_RUN_DIR`（默认是它旁边的 `run`）。复位模型只认训练原句（“Reset: …”），LEVI 把它们冻结在作业里（`subtask.reset_prompts`）。

## 绑定里的 argv

绑定 `policy_swap.services.reset` 的服务块（每个值都是独立参数、不含空白）：

```
[<openpi .venv 的 python>, lab/policy_loader/serve_loader.py,
 --config, reset_r1_step20000, --checkpoint-dir, <reset_r1 文件夹绝对路径>,
 --loader, reset_r1_loader:load, --port, <绑定里复位的端口>]
```

作业 `deployments.reset` 里写同样的 `config` 和 `checkpoint_dir`。`tests/test_policy_loader.py` 把这样的 argv 交给真实的 `fr3_policy_swap._service` 和 `runs_arm`，把服务的 metadata 交给真实的 `check_identity`。

## 装进 openpi（本仓库不做）

```
python3 integrations/policy_loader/install.py --openpi-root <openpi 检出>           # 干跑
python3 integrations/policy_loader/install.py --openpi-root <openpi 检出> --apply
```

只写 `lab/policy_loader/{serve_loader,reset_r1_loader}.py` 和 `installation-receipt.json`（openpi 检出的 `lab/` 被 git 排除，不改任何已有的 openpi 文件）；拒绝符号链接和内容不同的已有文件；要求 `src/openpi/serving/websocket_policy_server.py` 和 `scripts/serve_policy.py` 存在。装之前要取得 openpi 检出所有者的同意；装完按上文解包源码。

## 未完成

- GPU 冒烟测试（2026-11-16 已做，不涉及机器人）：真检查点的 `serve_loader.py` 和 11-14 的原型分别跑在回环端口 7873/7874，持 GPU 锁、`XLA_PYTHON_CLIENT_MEM_FRACTION=.22`，同一帧录下的观测各问 3 次。metadata 带 `config` 和 `checkpoint_dir`（原型两者都没有）；第一个块的 xyz 与原型最多差 0.14 mm，小于每个服务自身连问之间的差（约 2 mm）；预热后推理 59 ms，原型 58 ms。记录：`openpi/lab/policy_loader/smoke-20261116/result.json`。
- 客户端镜像 `serve_loader.client_problems` 是手工抄的 `_check_server`；客户端改了，测试看不出来。
- 设计里的真机验证 V1 到 V5 未做。
