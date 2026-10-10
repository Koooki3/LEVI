# Crash recovery / 崩溃后自动恢复

LEVI recovers from a **crash while it is running normally**. It does **not** start anything at boot, and it does not touch a service you stopped on purpose. / LEVI 只在**正常运行时发生崩溃**的情况下自动恢复；**不做开机自启**，也不会重启你有意停掉的服务。

## What restarts what / 谁重启谁

| What died / 什么崩溃了 | Restarted by / 由谁重启 | Not restarted when / 不会重启的情况 |
| --- | --- | --- |
| The core (`levi.agent.core`, port 7861) while `levi serve` runs / `levi serve` 运行时核心崩溃 | `levi serve` itself (`levi/watch.py`): it looks every 5 s; a core that left `agent/core/instance.json` behind with no process, and no stop request, was killed (signal, OOM) or crashed. At most 5 restarts in 10 minutes; after that it waits until the window has passed and says so once in its log | `levi stop`, the UI's stop, or a plain `kill` (SIGTERM): the core removes the file on the way out. Also held back, with the reason logged once: job workers (export, RECAP, segmentation labelling…) whose core died still run and a new core would stop them (the watch keeps looking and restarts the core when they have finished, nothing to do); runtime files were **committed** since `levi serve` started (a restarted core would run newer code than its web page: restart LEVI as a whole, `levi stop` and start it again; uncommitted edits do not count) |
| `levi serve` or its web page / `levi serve` 或网页进程崩溃 | A systemd user unit with `Restart=on-failure` (template below). `levi serve` exits 0 only when the web page was stopped by SIGTERM or SIGINT (`next start` exits 0 on them); a web page killed by anything else (SIGKILL, OOM, a hangup) or exiting with an error makes `levi serve` exit non-zero. `levi dev` keeps its old exit status | a stop on purpose: `levi stop`, then `systemctl --user stop levi-product` (or SIGTERM) |
| The live service supervisor (`levi live start`) / 实时服务的监督进程崩溃 | A systemd user unit with `Restart=on-failure`; the unit's cgroup is cleaned first, so a vLLM left behind by the crash is stopped and the GPU lock is free again | `levi live stop` or `systemctl --user stop levi-live` (both exit with status 0) |

Consequence of `KillMode=control-group` / 后果：when systemd restarts `levi serve`, it stops the whole unit first, **including the core and every job worker** (export, RECAP, segmentation); a job in progress is lost, and the next start reclaims its leftovers. So stop deliberately with `levi stop` (it refuses while jobs run) before `systemctl --user stop`. / systemd 重启 `levi serve` 时会先停掉整个单元，**包括核心和所有作业进程**，进行中的作业会丢；所以有意停止先用 `levi stop`（有作业时它会拒绝）。

The same rule decides how the page starts the live service / 同一条规则决定页面怎样启动实时服务：anything the product core starts lives in `levi-product.service`'s cgroup (`start_new_session` changes the session, not the cgroup) and is killed at the product's next restart. So the product LEVI starts the live service only with `systemctl --user start levi-live` (`LEVI_LIVE_UNIT`), never with `levi live start --daemon`, and stops it with `systemctl --user stop levi-live` or, for one started from a terminal, the SIGTERM `levi live stop` sends. Off unless `LEVI_LIVE_SERVICE_CONTROL=1`, and to stay off until the web UI's proxy checks `Host` and `Origin`; see [Starting and stopping the service from the page](LIVE.md#starting-and-stopping-the-service-from-the-page). / 产品核心启动的任何进程都在 `levi-product.service` 的 cgroup 里（`start_new_session` 只换会话，不换 cgroup），产品下次重启时会被杀掉。所以产品 LEVI 只用 `systemctl --user start levi-live`（`LEVI_LIVE_UNIT`）启动实时服务，从不用 `levi live start --daemon`；停止用 `systemctl --user stop levi-live`，终端启动的服务则发 `levi live stop` 发的 SIGTERM。默认关闭，设置 `LEVI_LIVE_SERVICE_CONTROL=1` 才打开；界面代理检查 `Host` 和 `Origin` 之前保持关闭，见[在页面上启动和停止实时服务](LIVE.zh-CN.md#在页面上启动和停止实时服务)。

Not covered / 不覆盖：a process that is alive but stuck (not answering), boot, power loss, and a full logout of your user: with systemd `Linger=no` the user's units stop at logout (`loginctl enable-linger $USER` keeps them if you want that). / 活着但卡住的进程、开机、断电、用户完全注销（`Linger=no` 时注销会停掉用户服务；想保留用 `loginctl enable-linger $USER`）。

## The units / 服务单元

Two `systemd --user` units, with **no `[Install]` section** so they cannot be enabled at boot. Paths below are examples; use your own checkout. / 两个 `systemd --user` 单元，**没有 `[Install]` 段**，不能设为开机自启。路径只是示例，换成你自己的检出。

```ini
# ~/.config/systemd/user/levi-product.service
[Unit]
Description=LEVI product (UI :7860, core :7861), crash recovery
StartLimitIntervalSec=900
StartLimitBurst=5
[Service]
Type=simple
WorkingDirectory=/path/to/LEVI
ExecStart=/path/to/LEVI/.venv/bin/levi serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
OOMPolicy=continue
ManagedOOMPreference=avoid
LimitNOFILE=1048576
```

```ini
# ~/.config/systemd/user/levi-live.service
[Unit]
Description=LEVI background live-annotation service, crash recovery
StartLimitIntervalSec=900
StartLimitBurst=4
[Service]
Type=simple
WorkingDirectory=/path/to/LEVI
ExecStart=/path/to/LEVI/.venv/bin/levi live start --auto-approve --prewarm --workspace /path/to/live-ws --config /path/to/live-ws/live.toml --root /path/to/rollouts
ExecStop=-/path/to/LEVI/.venv/bin/levi live stop --workspace /path/to/live-ws
Restart=on-failure
RestartSec=10
TimeoutStopSec=180
KillMode=control-group
OOMPolicy=continue
ManagedOOMPreference=avoid
LimitNOFILE=1048576
```

`OOMPolicy=continue` keeps one memory-killed process (the web page, the core) from taking the whole unit down; `levi serve` then restarts the core. / `OOMPolicy=continue` 避免单个进程被内存杀掉时整个单元一起停，核心由 `levi serve` 重启。

```bash
systemctl --user daemon-reload
systemctl --user start levi-product levi-live      # instead of `levi serve` / `levi live start --daemon`
systemctl --user status levi-product levi-live
levi stop && systemctl --user stop levi-product    # stop the product (levi stop refuses while jobs run)
systemctl --user stop levi-live                    # runs `levi live stop` first
```

**Reaching the page / 访问页面**：the unit serves the web page on `127.0.0.1:7860`, and the web bridge answers loopback names on any port plus the names you list, and takes writes only from the page itself ([API → Trust boundary of the web bridge](API.md#trust-boundary-of-the-web-bridge--网页桥接的信任边界)). From another machine use `ssh -L <any local port>:127.0.0.1:7860`; for a LAN name or a reverse proxy put `LEVI_UI_ALLOWED_HOSTS` in the checkout's `.env` (the unit's `levi serve` reads it) and restart the unit. Scripts write through the `levi` CLI, not through :7860. / 单元在 `127.0.0.1:7860` 提供网页，网页桥接接受任意端口上的 loopback 名字和你列出的名字，只收来自页面本身的写请求。从别的机器访问用 `ssh -L <本机任意端口>:127.0.0.1:7860`；用局域网名字或反向代理时，把 `LEVI_UI_ALLOWED_HOSTS` 写进检出的 `.env`（单元里的 `levi serve` 会读取）并重启单元。脚本经 `levi` 命令行写入，不经 :7860。

**Migrating from a terminal-started instance / 从终端启动的实例迁移**：`levi stop` first, then `systemctl --user start levi-product`; for the live service `levi live stop` first (a unit cannot start while a `levi live start --daemon` runs). Two instances on the same ports cannot coexist. / 先 `levi stop` 再用 systemd 启动，同一端口不能有两个实例。

After 5 (product) or 4 (live) crashes in 15 minutes systemd stops restarting and the unit stays `failed`: read `journalctl --user -u levi-product` / `-u levi-live` and the service logs, then `systemctl --user reset-failed` and start it again. / 15 分钟内崩溃 5 次（产品）或 4 次（实时服务）后 systemd 不再重启，单元保持 `failed`：看日志，`reset-failed` 后再启动。

**A caveat on this kind of machine / 注意**：if the user's inotify instances are used up (`journalctl --user` shows "Failed to add control inotify watch descriptor"; limit `fs.inotify.max_user_instances`, default 128), systemd cannot see that a unit's cgroup has emptied and waits for the whole `TimeoutStopSec` on every stop and every crash recovery. The timeouts above keep that wait bounded; raising the limit (`sudo sysctl fs.inotify.max_user_instances=512`) removes it. This is a system setting: LEVI does not change it. / 如果用户的 inotify 实例用尽，systemd 看不到单元的 cgroup 已空，每次停止和每次崩溃恢复都要等满 `TimeoutStopSec`；调高上限可以消除等待。这是系统设置，LEVI 不会去改。

## Verified / 已验证

`tests/test_core_watch.py` covers the watch (restart once per look, spacing, the cap and the retry after the window, a failing restart, the vetoes, the exit status of `levi serve`, runtime-file detection, the stop marker, a killed child that is still a zombie) and a real `python -m levi.agent.core` process: SIGTERM leaves no `instance.json`, SIGKILL counts as a crash. With a throw-away workspace on ports 7890/7891 under a transient user unit: `kill -9` of the core brought it back in about 6 s, `levi stop` was not undone, `kill -9` of `levi serve` restarted the whole product, `systemctl stop` stopped everything; with a throw-away live workspace: `kill -9` of the supervisor restarted it (the stale instance lock was taken over) and `levi live stop` was not undone. / 用临时工作区和临时端口在临时用户单元下验证过：杀核心约 6 秒恢复，`levi stop` 不被撤销，杀 `levi serve` 整个产品被拉起，`systemctl stop` 全部停止；实时服务：杀监督进程被拉起（接管了过期的实例锁），`levi live stop` 不被撤销。
