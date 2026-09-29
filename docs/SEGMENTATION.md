# Fast instance segmentation / 快速实例分割

LEVI can segment and track objects while an episode plays, and label whole datasets offline, with a small **student model** distilled from the SAM3 **teacher model**. The student (RF-DETR-Seg, Apache-2.0) runs at camera frame rate on both cameras at once; ByteTrack (MIT) keeps each object's instance id and concept label for the whole episode. SAM3 stays the quality path: it labels a dataset offline and produces the training frames a student learns from.

LEVI 可以在播放片段时实时分割并跟踪物体，也可以离线标注整个数据集。实时部分用的是从 SAM3 **教师模型**蒸馏出来的小型**学生模型**（RF-DETR-Seg，Apache-2.0），两路相机同时按相机帧率出结果；ByteTrack（MIT）在整个片段内保持每个物体的实例 ID 和概念标签。SAM3 仍是质量优先的路径：离线标注数据集，并为学生模型生成训练帧。

| Use / 用途 | Engine / 引擎 | Where / 入口 |
| --- | --- | --- |
| Live overlay while the episode plays / 播放时实时叠加 | student / 学生模型 | Episode viewer → Annotations → Objects → **Fast segmentation** → Live overlay |
| Label a dataset, fast / 快速标注数据集 | student | Fast segmentation → Label dataset → Fast |
| Label a dataset, best quality / 高质量标注数据集 | SAM3 teacher / SAM3 教师模型 | Fast segmentation → Label dataset → Quality (runs the [SAM3](SAM3.md) worker) |
| Train a student for a new task or new objects / 为新任务或新物体训练学生模型 | SAM3 → student | Fast segmentation → Distil a student |

All results are ordinary object annotations: the same sidecar, review states, Objects & Tracking panel and export as SAM3 ([SAM3](SAM3.md)). Model output is always `suggested` until a person reviews it; the source is `student` (or `sam3`).

所有结果都是普通的对象标注：与 SAM3 共用同一个 sidecar、审核状态、Objects & Tracking 面板和导出。模型结果在人工审核前一律是 `suggested`，来源记为 `student`（或 `sam3`）。

## Saving never erases other episodes / 保存不会覆盖其他片段

A run (student labelling, a live session, or a SAM3 run) writes one new sidecar revision that replaces only the (episode, camera) pairs it covered; every other episode's objects are kept (hard-linked from the previous revision). Before this release a SAM3 run replaced the whole sidecar, so labelling episode 5 erased episode 3's objects.

每次运行（学生模型标注、实时会话或 SAM3 运行）只写一个新的 sidecar 版本，只替换本次覆盖的（片段，相机）；其他片段的对象全部保留（从上一个版本硬链接）。此前 SAM3 运行会替换整个 sidecar，标注第 5 个片段会抹掉第 3 个片段的对象，本版已修正。

## Install / 安装

The student runs in its own environment; LEVI's core never imports Torch.

```bash
integrations/segmentation/setup.sh          # uv venv with torch cu128, rfdetr 1.11.0, supervision
export LEVI_SEG_WORKER_PYTHON=$PWD/integrations/segmentation/.venv/bin/python   # optional, this is the default
```

Distillation also needs the [SAM3](SAM3.md) worker and checkpoint. RF-DETR downloads its COCO starting weights (no token) into `$LEVI_WORKSPACE/checkpoints/segmentation/_base/` on first use.

蒸馏还需要 [SAM3](SAM3.md) worker 和 checkpoint。RF-DETR 第一次使用时把 COCO 预训练权重下载到 `$LEVI_WORKSPACE/checkpoints/segmentation/_base/`（不需要 token）。

## Live overlay / 实时叠加

1. Open an episode, **Annotations → Objects**, choose a student model and switch on **Live overlay**. LEVI starts one worker for this episode (both cameras) and answers once the model is loaded (a few seconds).
2. Play, pause, seek or change speed as usual. The page posts the player clock to the worker; the worker decodes the frame that is due (one frame ahead by default), runs the student on both cameras in one batch and tracks each camera.
3. The page sends each camera's own position with the time it read it, so the worker follows every camera even when their videos drift apart and removes the message's transport delay. Each camera has its own overlay canvas. On every animation frame it reads the video's own `currentTime` and draws that frame's result, or the newest earlier result no more than 3 frames old; an older result is dropped rather than shown late. The overlay never waits for the model, so playback never stalls.
4. Switching the overlay off, leaving the episode or closing the page stops the worker (a page that disappears without saying so is noticed after 2 minutes without a player clock; the page sends one every second, also while paused). With **Save results when stopping** on, the first result of every frame shown is saved as one revision for this episode's cameras. Stopping the LEVI service while an overlay runs discards what it has not saved: stop the overlay first to keep its results.

1. 打开片段，进入 **Annotations → Objects**，选择学生模型，打开 **实时叠加**。LEVI 为该片段启动一个 worker（两路相机），模型加载完成后（几秒）返回。
2. 照常播放、暂停、拖动或改变倍速。页面把播放器时钟发给 worker；worker 解码当前应显示的帧（默认提前 1 帧），两路相机合成一批推理，并各自跟踪。
3. 页面发送每路相机各自的位置和读取时刻，worker 分别跟随每路相机（即使两路视频有偏差），并扣除消息在路上的延迟。每路相机有独立的叠加画布。每个动画帧读取该视频自己的 `currentTime`，显示这一帧的结果；没有时显示不超过 3 帧的最新结果，更旧的结果直接丢弃，不会延迟显示。叠加层从不等待模型，播放不会卡顿。
4. 关闭叠加、离开片段或关闭页面都会停止 worker（页面每秒发送一次播放器时钟，暂停时也发；页面异常消失时，2 分钟收不到时钟即停止）。勾选 **停止时保存结果** 时，已显示各帧的第一次结果作为该片段相机的一个版本保存。

Instance ids are stable across frames and across a concept's single-frame flicker: each track votes on its concept, so one mislabelled frame changes neither the id nor the label. A seek restarts the tracker (new ids after the old ones).

实例 ID 跨帧稳定；单帧类别跳变也不会改变 ID 和标签，因为每条轨迹按多帧投票决定概念。拖动进度条会重启跟踪器（新 ID 接在旧 ID 之后）。

### Measured / 实测

Plates test set (10 episodes held out from training, both cameras, 3,550 frames), one RTX 5090 used by nothing else, student `plates-student-v1` in FP16. The recordings are 10 fps, so 30 fps playback is the video played at 3× (`bench` in the worker: the live pipeline with a synthetic player clock and a simulated display that shows the newest result not older than 3 frames).

在 plates 测试集上测量（10 个未参与训练的片段，两路相机，共 3550 帧），RTX 5090 独占，学生模型 `plates-student-v1`，FP16。录像是 10 fps，30 fps 回放即 3 倍速播放。

| Measure / 指标 | Decode one frame ahead (default) / 提前 1 帧解码（默认） | No decode-ahead / 不提前解码 |
| --- | --- | --- |
| Sustained rate per camera / 每路持续帧率 | 29.4–29.9 fps | 30.0 fps |
| Frames analysed / 处理帧占比 | ≥ 98 % (one frame per episode: the first) | 100 % |
| Skipped / dropped frames / 跳帧、丢帧 | 0 / 0 | 0 / 0 |
| End-to-end latency (decoded → mask ready), p50 per camera / 端到端延迟 p50 | 7–16 ms (median 12.5 ms) | 7–16 ms (median 11.8 ms) |
| Latency p95 (worst episode) / p95（最差片段） | 35 ms | 23 ms |
| Display shows the result of the frame on screen / 显示的正是当前帧的结果 | 99.6 % of display ticks | 0 % (always the previous frame, ≤ 3 frames old) |
| Inference, both cameras in one batch, p50 / p95 / 两路合批推理 | 6–8 ms / 9–18 ms | 6–7 ms / 9–14 ms |
| GPU memory (worker process) / 显存（worker 进程） | 1,436 MiB | 1,436 MiB |
| CPU (worker process, 32-thread 9950X3D) / CPU | 430–550 % | 430–480 % |
| Model load / 模型加载 | 3.8 s | 3.7 s |

Through the web UI (production build, headless Chromium, episode 72 at 3×): every sampled display tick drew the result of the frame on screen on both cameras; the worker analysed all 220 frames of each camera with no skipped frame, latency p50 8 / 16 ms and p95 15 / 19 ms (wrist / side). Offline labelling of the same 10 episodes, both cameras, as one LEVI job: 232 fps including decoding and mask encoding (15.6 s plus 3.9 s model load).

通过网页界面（生产构建，headless Chromium，片段 72，3 倍速）：两路相机每次采样时显示的都是屏幕上这一帧的结果；worker 处理了每路全部 220 帧，无跳帧，延迟 p50 8 / 16 ms，p95 15 / 19 ms（腕部 / 侧面）。离线标注同样 10 个片段的两路相机（一个 LEVI 作业）：232 fps（含解码和 mask 编码），15.6 s，另加模型加载 3.9 s。

## Label a dataset / 标注数据集

**Fast** runs the student over the chosen episodes (all by default) and both cameras in one job: a decode thread per item, batched FP16 inference, tracking in frame order, masks as COCO RLE. **Quality** runs the SAM3 worker with the student's concepts as text prompts. Both show progress and can be cancelled; a cancelled job saves nothing.

**快速** 用学生模型在一个作业里处理所选片段（默认全部）的两路相机：每项一个解码线程、FP16 批量推理、按帧序跟踪、mask 存为 COCO RLE。**高质量** 用 SAM3 worker，以学生模型的概念作为文本提示。两者都显示进度、都可取消；取消的作业不保存任何结果。

## Distil a student / 蒸馏学生模型

One job, one GPU lock for the whole chain:

1. **teacher** — the SAM3 image model labels every `stride`-th frame of the chosen episodes with the text concepts (one backbone pass per frame for all concepts; overlapping masks of different concepts are merged, the higher score wins), writing COCO train / valid / test folders;
2. **training** — RF-DETR-Seg is fine-tuned from its COCO weights;
3. **evaluating** — the student is scored on the held-out (test) episodes against the teacher's labels: AP50, AP50:95, class-agnostic recall and per-concept recall;
4. **registering** — `weights.pth` and `manifest.json` go to `$LEVI_WORKSPACE/checkpoints/segmentation/<name>/`; the model is listed only once its manifest is complete (a failed or cancelled run leaves nothing behind).

The manifest records the concepts (class order), teacher and its settings, the training / validation / held-out episodes of every source dataset, the held-out scores, tracker settings, LEVI commit and licences. Sources may include other datasets — for example teleoperated demonstrations in which a person's hand is in view — so the student learns objects the policy rollouts rarely show.

整条链一个作业、持一次 GPU 锁：SAM3 图像模型按 `stride` 抽帧并用文本概念伪标注（每帧所有概念共用一次 backbone，不同概念的重叠 mask 保留高分者），写成 COCO train / valid / test；RF-DETR-Seg 从 COCO 权重微调；在留出片段上对照教师标注评估（AP50、AP50:95、类别无关召回、各概念召回）；最后写入权重和 manifest。manifest 记录概念顺序、教师及参数、每个来源数据集的训练/验证/留出片段、留出指标、跟踪参数、LEVI 提交和许可。来源可以包含其他数据集，例如画面中有人手的遥操作示教，这样学生模型也能学到策略 rollout 里少见的物体。

Held-out scores are against the teacher, not against human labels: a student can at best match SAM3.

留出指标是对照教师模型的，不是对照人工标注：学生模型的上限就是接近 SAM3。

### The plates student / plates 学生模型

`plates-student-v1` was distilled inside LEVI (one job, 59 minutes: teacher 24 min, training 35 min, scoring 18 s) for the concepts *pink plate, white plate, green plate, blue plate, cup, robot arm, human hand*:

- sources: 30 policy rollouts of `stack_the_plates_of_same_color_together` for training (4 of them with a hand in view; 3 more for validation), and 14 online-RL plates episodes in which a person's hand is in view (11 train, 1 validation, 2 held out) — found by a SAM3 "human hand" scan of 112 rollouts, 36 teleoperated demonstrations and 140 sampled online-RL episodes, checked by eye (the teleoperated demonstrations show no hands; in wrist views SAM3 also calls an eggplant tip or a pink plate edge a hand);
- teacher frames: 6,516 train / 410 validation / 1,610 held-out (every 3rd frame; every 2nd for the hand episodes);
- held-out scores against the teacher (10 rollout test episodes + 2 hand episodes): AP50 0.938, AP50:95 0.839, class-agnostic recall 0.978; recall per concept — pink 0.99, white 0.96, green 0.99, blue 1.00, cup 0.99, robot arm 0.92, human hand 0.78 (precision 0.70, 156 instances);
- against the benchmark's hand-checked reference frames of the same test episodes (40 frames, 298 instances): AP50 0.927 / AP 0.864, versus 0.817 / 0.759 for the benchmark's student trained on rollouts only; tracking against SAM3's tracks: 2.1 identity switches per 100 frames (2.3 before), IDF1 0.72 (0.70), MOTA 0.80 (0.72).

`plates-student-v1` 在 LEVI 内蒸馏（一个作业，59 分钟：教师 24 分钟，训练 35 分钟，评估 18 秒）。训练来源为 30 个策略 rollout（其中 4 个有人手；另 3 个做验证），加上 14 个画面中有人手的 online RL plates 片段（通过 SAM3 "human hand" 扫描 112 个 rollout、36 个遥操作示教和 140 个抽样 online RL 片段找到，并逐张目检；遥操作示教里没有人手；腕部视角下 SAM3 也会把茄子尖或粉色盘沿判为人手）。留出集对照教师：AP50 0.938，AP50:95 0.839；人手召回 0.78、精度 0.70。对照基准中人工核对过的参考帧：AP50 0.927 / AP 0.864（只用 rollout 训练的基准学生模型为 0.817 / 0.759）；对照 SAM3 轨迹，每 100 帧 2.1 次 ID 切换（原 2.3），IDF1 0.72，MOTA 0.80。

## Licences and model sources / 许可与模型来源

| Component / 组件 | Licence / 许可 | Shipped / 是否随 LEVI 分发 |
| --- | --- | --- |
| RF-DETR-Seg N/S/M code and COCO weights (`rfdetr==1.11.0`) | Apache-2.0 (XL/2XL are licensed differently and never used) | installed by `setup.sh`, weights downloaded at first use |
| ByteTrack (`supervision`) | MIT | installed by `setup.sh` |
| A student distilled in LEVI | Apache-2.0 (recorded in its manifest) | stays in the workspace |
| SAM3 teacher weights | SAM License (Meta), whichever mirror they come from | optional, only for distillation and quality labelling |

No AGPL or non-commercial component is part of the fast path (Ultralytics YOLO and SAM-MT were benchmarked and rejected for their licences, among other reasons).

快速路径不包含任何 AGPL 或非商用组件（Ultralytics YOLO 和 SAM-MT 在选型基准中因许可等原因被排除）。

SAM3 weights: LEVI downloads `sam3.pt` from the public mirror `1038lab/sam3` by default (`LEVI_SAM3_MODEL_REPO`, `LEVI_SAM3_MODEL_FILENAME`). The official `facebook/sam3` repository is gated: request access on Hugging Face with your account, accept the SAM License, then provide a read token (`HF_TOKEN` or `hf auth login`). The SAM License applies whichever source the weights come from.

SAM3 权重：LEVI 默认从公开镜像 `1038lab/sam3` 下载 `sam3.pt`（可用 `LEVI_SAM3_MODEL_REPO`、`LEVI_SAM3_MODEL_FILENAME` 配置）。官方 `facebook/sam3` 需要审批：用自己的 Hugging Face 账号申请访问并接受 SAM License，再在本机提供只读 token（`HF_TOKEN` 或 `hf auth login`）。不论从哪个来源获取，都适用 SAM License。

## HTTP API

Under the annotation service (`/annotations/api/…` on the core port, `/api/annotation/…` through the web UI); every call takes the dataset as `repo_id` (query or body).

| Route | |
| --- | --- |
| `GET segmentation/status` | worker and teacher readiness, models, cameras, recent jobs, live sessions |
| `GET segmentation/models`, `DELETE segmentation/models/{name}` | the student store |
| `POST segmentation/label` | `{model, episodes?, cameras?, batch?}` → job |
| `POST segmentation/distil` | `{name, concepts, sources: [{repo_id?, train, valid, test, stride?}], architecture?, epochs?, stride?, confidence?}` → job |
| `GET segmentation/jobs/{id}`, `POST segmentation/jobs/{id}/cancel` | progress, result, cancel |
| `POST segmentation/live` | `{episode_index, model, cameras?, save?, lead_frames?}` → session (once the model is loaded) |
| `POST segmentation/live/{id}/clock` | `{playing, time, rate}` (episode-local seconds) |
| `GET segmentation/live/{id}/events` | server-sent events: `result` (one camera frame: objects with `track_id`, `concept`, `score`, `bbox_xyxy`, `mask_rle`), `stats`, `ready`, `stopped`, `error`, `closed`; a slow reader gets only the newest result per camera |
| `POST segmentation/live/{id}/stop` | stop and save |

Agents read the same state with the read-only capability `segmentation.status` ([Agents](AGENTS.md)).

## Settings / 设置

| Setting | Default | |
| --- | --- | --- |
| `LEVI_SEG_WORKER_PYTHON` | `integrations/segmentation/.venv/bin/python` | the student environment |
| `LEVI_SEG_MODEL_DIR` | `$LEVI_WORKSPACE/checkpoints/segmentation` | the student store (inside the workspace) |
| `LEVI_GPU_LOCK_FILE` | unset | an `flock` file shared with other GPU users on the machine: labelling and distillation jobs queue for it (their progress stays fresh while they wait); a live session that cannot take it at once fails with that reason |
| `LEVI_GPU_LOCK_TIMEOUT_SECONDS` | `14400` | how long a job waits for that lock |
| `LEVI_SEG_MIN_FREE_MIB` / `LEVI_SEG_DISTIL_MIN_FREE_MIB` / `LEVI_SEG_LIVE_MIN_FREE_MIB` | 3000 / 14000 / 2500 | free GPU memory a labelling job / distillation / live session needs (with a GPU lock set, a job only warns) |
| `LEVI_SEG_LIVE_MAX_SESSIONS` | `2` | concurrent live sessions (one per episode) |
| `LEVI_SEG_LIVE_IDLE_SECONDS` | `120` | a live session without a player clock stops (and saves) |
| `LEVI_SEG_TIMEOUT_SECONDS` / `LEVI_SEG_STALL_SECONDS` | 43200 / 1800 | a job running longer, or without progress for longer, is stopped |

`LEVI_CPU_ONLY=1` refuses every segmentation model. The `fake` provider (CPU tests) runs the same worker with discs in place of model output.

## Limits / 已知限制

- Recorded plates videos are 10 fps; 30 fps playback was measured by playing them at 3× speed, which moves objects three times as far between frames as a real 30 fps camera (harder for tracking).
- A student knows only its concepts and the looks its training frames showed; a new task or new objects need a new distillation (about 30–40 minutes on one RTX 5090 for ~40 episodes).
- The Robotiq gripper is not found by any text prompt; it is part of "robot arm".
- Concept by colour can be wrong where the teacher is wrong: a pink plate pushed by a hand was labelled white in one checked frame; the wrist camera's cyan cast is the hardest case.
- While the live overlay runs on an episode, the stored objects of that episode are hidden; the overlay stops when the Annotations tab is left.
- Identity switches: about 2 per 100 frames against SAM3 tracks on the plates test set (SAM3 itself is the reference). Objects that leave and re-enter the view get a new id once the tracker has lost them for longer than its lost buffer (`tracker.lost_buffer` in the manifest).
