# FR3 manual protocol integration / 手动控制协议

Offline artifacts only. No controller/ROS/service/robot access is performed by these files or tests.
本目录只准备离线协议与补丁；未做真机验证，不启动或重启任何服务。

`protocol.py` is stdlib-only. `adapter.py` is imported only inside the deployed ROS package.
To deploy after review, verify every shared file against `baseline_hashes.json`, apply its unified patch,
and install these two new helper files into that same `fr3_robot_server` package as
`fr3_manual_protocol.py` and `fr3_manual_adapter.py`. A mismatching baseline requires a new review;
do not force the patch or replace whole shared files. Existing local changes are part of the hashed baseline.
Root driver owns shared edits and deployment. No deployment has been performed here.

## Contract

All responses include `ok`; rejected mutations return 409 with `error`. Request object keys are exact;
unknown/missing keys, nonfinite numbers and booleans used as numbers are rejected. All POST manual routes
require the direct peer IP to be loopback; forwarded headers are ignored. There is no HTTP retry.
Clock is the server host's `time.monotonic`, not browser clock or wall time. Client must share host clock.

- GET `/manual/v1/capabilities`: `protocol_version:1`, `instance_id`, `monotonic_time`,
  `clock_domain:same_host_monotonic`, translation/rotation/gripper capability and limitations.
- POST `/manual/v1/start`: `{session_id,limits,stop_limits}`. `limits` has exactly all existing
  `levi.marvel0.manual.Limits` fields. `stop_limits` explicitly supplies positive finite
  `settle_window_s,settle_position_m,settle_angle_rad,gripper_settle_rad`; no physical defaults.
  Returns state including measured anchor sample. Start publishes nothing and activates/homes nothing.
- POST `/manual/v1/heartbeat`: `{session_id,sequence,expires_at_mono}`. Sequences are globally strictly
  increasing across heartbeat/pose/gripper within one session. Expiry must be future and at most
  `limits.target_timeout_s` ahead; heartbeat additionally caps its deadman by `deadman_timeout_s`.
- POST `/manual/v1/pose`: same envelope plus `pose7:[x,y,z,qx,qy,qz,qw]`. Base frame translation only;
  quaternion must exactly match the measured anchor quaternion. Anchor age, feedback age, box, anchor
  displacement, step, velocity budget, period and measured orientation drift are enforced server-side.
  Admission and physical publish share one lock. One publication is in flight; no pending target queue.
- POST `/manual/v1/gripper`: same envelope plus finite `opening_ratio` in `[0,1]` (0 closed, 1 open).
  Uses a direct finite Robotiq action, bypassing the legacy reversal/debounce queue. Busy requests reject.
  Deadline expires independently of HTTP. Pending acceptance is cancelled when it eventually arrives.
- POST `/manual/v1/stop`: `{session_id}` closes admission permanently for that session, cancels its
  gripper action and publishes the current fresh measured arm pose as hold. It never calls `/stopimp`.
  A hold publication or cancel acknowledgement never counts as measured physical stop.
- POST `/manual/v1/end`: `{session_id}` releases server ownership only after post-stop fresh arm and
  gripper feedback has stayed within explicitly supplied tolerances for the whole settle window, and
  the gripper action has an actual terminal result status. Feedback gaps/movement reset the window.
  Cancel can be acknowledged while the hardware continues moving: continue waiting for measured settle.
  Unknown result, stale feedback, failed hold or timeout retains ownership and blocks legacy mutation.
- GET `/manual/v1/state`: instance/session identity, `admission_open`, `stop_evidence`, reason,
  `sample:{pose7,joints,frame,seq,joint_seq,monotonic_time,joint_monotonic_time,age_s}`,
  measured gripper `{opening_ratio,theta,seq,monotonic_time,terminal,action_status}` and `in_flight`.
  `seq` is receipt sequence, timestamps are monotonic local receipt times, not hardware clock stamps.

Watchdog runs separately from HTTP and closes an expired session. Legacy POST handler execution and
manual start use the same lock; an earlier legacy handler completes before start can own the arm.
During manual ownership, including fault/deadman expiry, every legacy POST route is blocked atomically.
The adapter refuses start if a legacy gripper request is active/pending, or idle cannot be proven.
Robotiq is supported; other gripper backends are not silently assumed safe.

## Boundaries / 限制

Measured settling is evidence within explicit tolerances, not a certified emergency stop or brake.
The existing ROS topic transport may contain earlier ordered publications; the last publication is hold,
and end waits for subsequent measured settle. No claim that middleware purges subscriber buffers is made.
Freshness is receive-time freshness; delayed/replayed upstream ROS telemetry is not cryptographically detected.
The operator must explicitly provide a safe box and thresholds: no collision model is added by this adapter.
The existing server startup activation behavior is unchanged; installing files does not start that server.
No browser pointer event, HTTP success, action acceptance or cancel ACK is physical stop evidence.

Offline check (controller was explicitly stated OFF for this task):
`PYTHONDONTWRITEBYTECODE=1 nice -n 19 taskset -c 20-23 python3 -B -m unittest discover -s integrations/fr3_manual -p test_protocol.py -v`

## Offline verification / 离线验证

The deterministic suite covers watchdog expiry, stale feedback, replay, nonfinite inputs, box/step/rate
and rotation bounds, expiry immediately before dispatch, stop/publish serialization, atomic legacy
exclusion, hold failure, fresh post-stop settling, single gripper goal/no reversal queue, terminal result
versus cancellation acknowledgement, and acceptance arriving after cancellation. Tests use no ROS or
HTTP transport. Shared patch postimages are syntax-compiled in memory; actual baseline hashes are
recorded separately. ROS imports/action APIs and Flask wiring still require deployment review and a
human-approved short-range hardware acceptance run. No hardware verification claim is made.

Rejected requests that passed identity/deadline admission consume their sequence. Clients should use
one globally monotonic counter and never retry a mutation. A new session cannot replace a fault-locked
session; recover server/hardware ownership explicitly with human review if fresh stop proof is unavailable.

## Installation / 安装

The installer requires an explicit authorized package directory; it has no machine-specific target,
archive location, environment lookup or home-directory default. Supplying a path configures this offline
tool and does not grant permission to modify that directory. Obtain approval for the actual package and
archive root before applying. Only the two baseline-matched server files and the two named helpers are
within its change scope.

Read-only check (also the default when `--check` is omitted):

```sh
python3 integrations/fr3_manual/install.py --check --package /absolute/path/to/fr3_robot_server
```

After review, use an existing authorized archive root and a new child backup directory:

```sh
python3 integrations/fr3_manual/install.py --apply --package /absolute/path/to/fr3_robot_server \
  --archive-root /absolute/path/to/authorized/archive \
  --archive /absolute/path/to/authorized/archive/new-backup-directory
```

The installer rejects symlinks and unexpected changes, preserves original file modes, and records backups
and write-ahead pending entries before each replacement. Replacement is atomic per file, not across all
files. On failure, inspect every planned target's actual original/post hash and mode; never infer recovery
from the `installed` list alone. It never starts controllers or restarts services.

必须显式指定已获授权的 `--package`，没有机器路径默认值；默认只读核对。审查后安装还须明确
`--archive-root` 和其中全新的 `--archive` 子目录。路径参数不代替修改授权。逐文件先备份并记录
待写入项；部分失败须检查全部计划文件的实际哈希和权限，不能仅依赖已安装名单，不可直接重启服务。
